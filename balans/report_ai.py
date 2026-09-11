"""AI prioritizes verified facts and supported budgeting actions, never arithmetic."""
import json
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field
from balans.report_data import amount,fmt

PROMPT=Path(__file__).with_name('prompts').joinpath('report_v1.txt').read_text()


class AnalysisResult(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    observations:list[str]=Field(max_length=3)
    recommendations:list[str]=Field(max_length=3)
    limitation:str=Field(max_length=300)


class AnalysisPlan(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    fact_ids:list[str]=Field(min_length=1,max_length=3)
    action_ids:list[str]=Field(min_length=1,max_length=3)


def choices(snapshot):
    symbol='₽' if snapshot.get('currency','RUB')=='RUB' else snapshot['currency']
    summary=snapshot['summary']
    facts={'recorded_total':f"Число расходов за выбранный период: {summary['count']}. Сумма: {fmt(summary['total'])} {symbol}."}
    actions={'complete_records':'Проверьте, все ли расходы за период внесены. Полный учёт поможет выбрать реалистичный бюджет.'}
    if summary['categories']:
        top=summary['categories'][0]
        facts['largest_category']=f"Крупнейшая категория — «{top['name']}»: {fmt(top['total'])} {symbol} ({top['share']}% записанных расходов)."
        actions['plan_top_category']=f"Просмотрите покупки в категории «{top['name']}» и задайте план на следующий период с учётом обязательных потребностей."
    if any(c['name']=='Другое' for c in summary['categories']):
        facts['uncategorized']='Часть покупок отнесена к категории «Другое».'
        actions['review_other']='Уточните категории покупок из «Другое», чтобы видеть структуру расходов точнее.'
    if not amount(snapshot['previous']['total']):
        facts['no_comparison']='В периоде сравнения нет записанных расходов. По этим данным нельзя сделать вывод о росте трат.'
    else:
        facts['comparison']=f"Разница с периодом {snapshot['previous_start']} — {snapshot['previous_end']}: {fmt(snapshot['delta'])} {symbol} ({snapshot['delta_percent']}%). Сравнение касается записанных расходов."
        actions['check_comparison']='Перед выводами об изменении расходов проверьте полноту записей и сопоставимость периодов.'
    if summary['count']<10:
        facts['small_sample']='В периоде мало записей: выводы о привычках и устойчивых тенденциях делать рано.'
    return facts,actions


class ReportAI:
    def __init__(self,client,model):self.client=client;self.model=model
    @property
    def available(self):return self.client is not None
    def analyze(self,snapshot):
        facts,actions=choices(snapshot)
        request={key:snapshot[key] for key in ('start','end','previous_start','previous_end','summary','previous','delta','delta_percent')}
        request.update(facts=facts,actions=actions)
        response=self.client.with_options(timeout=30,max_retries=0).responses.parse(model=self.model,store=False,max_output_tokens=500,
            input=[{'role':'system','content':PROMPT},{'role':'user','content':json.dumps(request,ensure_ascii=False)}],text_format=AnalysisPlan)
        if response.status!='completed' or response.output_parsed is None:raise ValueError('Incomplete response')
        plan=AnalysisPlan.model_validate(response.output_parsed)
        if any(key not in facts for key in plan.fact_ids) or any(key not in actions for key in plan.action_ids):raise ValueError('Unsupported analysis')
        return AnalysisResult(observations=[facts[key] for key in dict.fromkeys(plan.fact_ids)],recommendations=[actions[key] for key in dict.fromkeys(plan.action_ids)],
                              limitation='Выводы относятся только к записям бота. Обязательства и полнота учёта неизвестны; расходы сами по себе не показывают финансовое положение.')
