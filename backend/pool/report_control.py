import logging
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from .audit import record
from .cohorts import count_cohorts, load_wave
from .constants import MIN_COHORT, MIN_TOTAL, ReportState, WaveState
from .factor_math import UnfitObservation, identify_factors
from .form_models import FormItem, Item
from .report_models import FactorReport
from .settings import QUEUE_KEY, REDIS_URL
from .survey_models import Survey

logger = logging.getLogger("pool.reports")

ACTIVE_STATES = (ReportState.QUEUED, ReportState.READY, ReportState.APPROVED)


def get_report(db, report_id, *, lock=False):
    query = select(FactorReport).where(FactorReport.id == report_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise HTTPException(404, "找不到因子核校单。")
    return row


def display_report(row):
    return {"id": row.id, "wave_id": row.wave_id, "code": row.code, "state": row.state,
            "reviewed_by": row.reviewed_by, "review_note": row.review_note,
            "findings": None if row.state == ReportState.VOID else row.findings}


def _open_fit(db, wave, actor):
    report = FactorReport(wave_id=wave.id, code="FA-" + uuid4().hex[:10].upper(),
                          state=ReportState.QUEUED, requested_by=actor)
    db.add(report)
    wave.state = WaveState.ANALYZING
    return report


def _wake(report):
    try:
        Redis.from_url(REDIS_URL, socket_timeout=3, socket_connect_timeout=2).lpush(QUEUE_KEY, report.id)
    except RedisError:
        logger.warning("Database request retained: %s", report.code)


def queue_report(db, wave_id, actor):
    wave = load_wave(db, wave_id, lock=True)
    if wave.state != WaveState.COLLECTING:
        raise HTTPException(409, "本轮已冻结，不能重复提报。")
    counts = count_cohorts(db, wave.id)
    missing = [item.code for item, size in counts if size < MIN_COHORT]
    if missing:
        raise HTTPException(409, f"教练组 {'、'.join(missing)} 未达到 {MIN_COHORT} 份匿名门槛，禁止合并。")
    if sum(size for _, size in counts) < MIN_TOTAL:
        raise HTTPException(409, f"合并建模需要至少 {MIN_TOTAL} 份完整观察。")
    report = _open_fit(db, wave, actor)
    record(db, actor, "freeze_for_fit", wave.code)
    db.commit()
    _wake(report)
    return report


def eligible_for_fit(db, wave_id):
    counts = count_cohorts(db, wave_id)
    return (all(size >= MIN_COHORT for _, size in counts)
            and sum(size for _, size in counts) >= MIN_TOTAL)


def withdraw_survey(db, wave_id, ticket, actor):
    # Lock order is wave -> report everywhere so withdrawal cannot deadlock a running fit or a signature.
    wave = load_wave(db, wave_id, lock=True)
    fingerprint = sha256(ticket.lower().encode()).hexdigest()
    survey = db.scalar(select(Survey).where(Survey.wave_id == wave.id,
                                            Survey.ticket_hash == fingerprint).with_for_update())
    if survey is None:
        raise HTTPException(404, "本轮没有这张观察券的答卷，无法撤回。")
    if survey.withdrawn:
        # Retrying a withdrawal is a no-op: the ticket is not consumed a second time.
        db.commit()
        return {"accepted": True, "already_withdrawn": True, "wave_code": wave.code,
                "wave_state": wave.state, "invalidated": [], "recalculated": False,
                "report_id": None}
    survey.withdrawn = True
    reports = db.scalars(select(FactorReport).where(FactorReport.wave_id == wave.id)
                         .order_by(FactorReport.id).with_for_update()).all()
    invalidated, had_fit = [], False
    for existing in reports:
        if existing.state not in ACTIVE_STATES:
            continue
        had_fit = True
        existing.state = ReportState.VOID
        invalidated.append(existing.code)
    recalculated = False
    if had_fit and wave.state != WaveState.SEALED:
        # A sealed round never reopens for supplementary collection, even below threshold.
        if eligible_for_fit(db, wave.id):
            report = _open_fit(db, wave, actor)
            recalculated = True
        else:
            wave.state = WaveState.COLLECTING
    record(db, actor, "withdraw_survey", wave.code)
    db.commit()
    if recalculated:
        _wake(report)
    return {"accepted": True, "already_withdrawn": False, "wave_code": wave.code,
            "wave_state": wave.state, "invalidated": invalidated,
            "recalculated": recalculated, "report_id": report.id if recalculated else None}


def do_pending(db):
    candidate = db.scalar(select(FactorReport.id).where(FactorReport.state == ReportState.QUEUED)
                          .order_by(FactorReport.id).limit(1))
    if candidate is None:
        return False
    wave_id = db.scalar(select(FactorReport.wave_id).where(FactorReport.id == candidate))
    # Wave first, then report: same order as withdrawal and signature.
    wave = load_wave(db, wave_id, lock=True)
    pending = db.scalar(select(FactorReport).where(FactorReport.id == candidate).with_for_update()
                        .execution_options(populate_existing=True))
    if pending is None or pending.state != ReportState.QUEUED:
        # A concurrent withdrawal voided this fit while we waited for the locks.
        db.rollback()
        return True
    items = db.scalars(select(Item).join(FormItem, FormItem.item_id == Item.id)
                       .where(FormItem.form_id == wave.form_id).order_by(FormItem.position)).all()
    surveys = db.scalars(select(Survey).where(Survey.wave_id == wave.id, Survey.withdrawn.is_(False))
                         .order_by(Survey.id)).all()
    try:
        pending.findings = identify_factors(items, surveys)
    except UnfitObservation as error:
        pending.state, pending.review_note, wave.state = ReportState.FAILED, str(error), WaveState.COLLECTING
    else:
        pending.state, wave.state = ReportState.READY, WaveState.REVIEW
    record(db, "worker", "factor_" + pending.state, pending.code)
    db.commit()
    return True


def approve(db, report_id, note, actor):
    wave_id = db.scalar(select(FactorReport.wave_id).where(FactorReport.id == report_id))
    if wave_id is None:
        raise HTTPException(404, "找不到因子核校单。")
    load_wave(db, wave_id, lock=True)
    report = get_report(db, report_id, lock=True)
    if report.state != ReportState.READY:
        raise HTTPException(409, "须先完成待核校的结果。")
    if actor == report.requested_by:
        raise HTTPException(409, "请交由不同岗位核校。")
    report.state, report.review_note, report.reviewed_by = ReportState.APPROVED, note, actor
    record(db, actor, "approve_factor_review", report.code)
    db.commit()
    return report
