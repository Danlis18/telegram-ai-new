import json
from openai import AsyncOpenAI
from app.config import settings

client = AsyncOpenAI(api_key=settings.openai_api_key)

SYSTEM_PROMPT = """Ти редактор українського спортивного Telegram-каналу.
Перетворюй вхідну новину на самостійний короткий Telegram-пост українською.
Стиль: живий, професійний, спортивний, природний; сильний перший рядок; короткі абзаци; доречні emoji.
Не копіюй формулювання джерела. Не вигадуй фактів, цитат, рахунків, дат чи трансферних сум.
Прибирай рекламу, промокоди, CTA та згадки про канал-джерело.
Якщо даних недостатньо або новина сумнівна — publish=false.
Поверни тільки JSON: {"publish":bool,"score":0-100,"text":"...","reason":"..."}.
"""

async def rewrite_news(text: str, source: str) -> dict:
    response = await client.responses.create(
        model=settings.openai_model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Джерело: @{source}\n\nНовина:\n{text}"},
        ],
    )
    raw = response.output_text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    return json.loads(raw)
