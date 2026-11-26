import {empty,headline,waveOptions} from '../components/desk.js';
import {questions,readScale} from '../components/scale.js';
import {esc} from '../format.js';
import {formData,handle,inlineError} from '../form.js';
import {actor,api} from '../store.js';

export async function surveyPage(root,ctx) {
  const [waves,forms] = await Promise.all([api().waves(),api().forms()]);
  const open = waves.filter(wave=>wave.state==='collecting');
  const active = open.find(wave=>wave.id===Number(ctx.query.get('id'))) || open[0];
  const form = forms.find(row=>row.id===active?.form_id);
  const technician = actor().role === 'technician';
  root.innerHTML = `${headline('匿名观察','提交班次中看到的课堂参与情况。使用无身份信息的一次性观察券。')}
   ${!technician ? empty('匿名观察由记录岗提交。原始逐人答案没有查询页面。') : `
   ${!active || !form ? empty('没有开放的观察轮次。请到班组回收检查状态。') : `
    <div class="survey-wrap">
    <aside class="survey-guide">
      <label>观察轮次<select id="select-wave">${waveOptions(open,active?.id)}</select></label>
      <h2>${esc(form.edition)}</h2><p>1 代表很少出现，5 代表经常出现。按观察原样作答，后台会根据版本处理反向条目。</p>
      <small>观察券只用于同轮去重，请勿录入姓名和联系方式。</small>
    </aside>
    <form id="survey-form" class="pad">
      <div class="two"><label>教练组<select name="cohort_id">${active.cohorts.map(row=>`<option value="${row.id}">${esc(row.label)}</option>`).join('')}</select></label>
      <label>观察券<input name="ticket" minlength="16" maxlength="32" required placeholder="16–32 位十六进制"></label></div>
      ${questions(form.items)}
      <label class="check"><input type="checkbox" name="consent" required> 同意只以合并形式使用本份观察</label>
      <button type="submit">送入匿名回收簿</button>${inlineError}</form>
    </div>`}
    <div class="survey-wrap">
    <aside class="survey-guide"><h2>撤回观察</h2><p>凭提交时的原观察券撤回一份答卷。用到该答卷的因子报告立即失效，排队或正在跑的拟合一并作废。</p>
      <small>重复撤回同一张券不会再作废报告，也不会重复扣除回收份额。</small></aside>
    <form id="withdraw-form" class="pad">
      <div class="two"><label>观察轮次<select name="wave_id">${waveOptions(waves,Number(ctx.query.get('id'))||waves[0]?.id)}</select></label>
      <label>原观察券<input name="ticket" minlength="16" maxlength="32" required placeholder="16–32 位十六进制"></label></div>
      <button type="submit">撤回该份答卷</button>${inlineError}</form>
    </div>`}`;
  const selector = root.querySelector('#select-wave');
  if (selector) selector.onchange = event => { location.hash = `survey?id=${event.target.value}`; };
  handle(root.querySelector('#survey-form'),async formElement=>{
    const {cohort_id,ticket} = formData(formElement,['cohort_id']);
    await api().submit({wave_id:active.id,cohort_id,ticket,consent:formElement.elements.consent.checked,answers:readScale(formElement)});
    ctx.refresh('匿名观察已接收。可继续提交下一份。');
  });
  handle(root.querySelector('#withdraw-form'),async formElement=>{
    const data = formData(formElement,['wave_id']);
    const result = await api().withdraw(data);
    if (result.already_withdrawn) return ctx.refresh('该观察券已撤回，无需重复操作。');
    if (result.recalculated) return ctx.refresh('答卷已撤回，旧报告已作废；余下答卷仍达标，已重新送入拟合核校。');
    if (result.invalidated.length) return ctx.refresh('答卷已撤回，相关因子报告已作废；本轮可继续补采。');
    ctx.refresh('答卷已撤回，本轮回收数量已更新。');
  });
}
