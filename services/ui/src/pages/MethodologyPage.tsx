/** Страница 5 «Методология»: как система понимает, что это слабый сигнал (§7 HANDOFF_UI). */

import { useEffect } from "react";
import { cssVars } from "../lib/style";

const STEPS = [
  "Запрос расширяется в поисковые фразы на русском и английском языках.",
  "Собираются документы из семи открытых источников с соблюдением их правил и лимитов.",
  "Документы очищаются от дубликатов и группируются по темам.",
  "Для каждой темы считаются 25 интерпретируемых признаков.",
  "Восемь детерминированных правил отсекают зрелые технологии, хайп и шум.",
  "Оставшиеся кандидаты получают оценку калиброванной модели и ранжируются.",
  "Для ТОП-N формируются описание, преимущество и кейс-пример по найденным источникам.",
];

const TRUST_RULES: { level: string; tone: string; strength: number; description: string }[] = [
  {
    level: "Высокая",
    tone: "high",
    strength: 3,
    description: "Научные публикации, препринты, патенты, государственные источники, стандарты",
  },
  {
    level: "Средняя",
    tone: "medium",
    strength: 2,
    description: "Отраслевые медиа, репозитории кода, энциклопедии, вакансии, аналитические отчёты",
  },
  {
    level: "Пониженная",
    tone: "low",
    strength: 1,
    description: "Пресс-релизы, корпоративные блоги, новостные агрегаторы",
  },
];

const EXCLUSION_RULES = [
  ["Не по теме", "Близость к запросу ниже 0,30 — кандидат не относится к направлению"],
  ["Нет доверенного источника", "Все документы кластера имеют пониженную доверенность"],
  ["Единственный источник", "Кандидат подтверждён одним документом без высокой доверенности"],
  ["Зрелая статья в энциклопедии", "Статья старше 3 лет и более 20 000 просмотров в месяц"],
  ["Сформированный рынок", "Лексика массового внедрения вместе с упоминанием крупных вендоров"],
  ["Лексика зрелости", "Преобладают слова «стандарт», «массовое внедрение», «доля рынка»"],
  ["Маркетинг доминирует", "Более 60 % пресс-релизов и блогов при отсутствии научных источников"],
  ["Хайп без подтверждения", "Лексика «революционный», «не имеет аналогов» без доверенных источников"],
];

const FEATURE_GROUPS: { name: string; count: number; tone: string; description: string }[] = [
  {
    name: "Лексические",
    count: 8,
    tone: "lexical",
    description: "Ранняя стадия, зрелость, хайп, упоминания крупных компаний, годы, финансирование",
  },
  {
    name: "Коллекционные",
    count: 11,
    tone: "collection",
    description: "Доли типов источников, рост числа публикаций, медианы цитирований и свежести",
  },
  {
    name: "Энциклопедические",
    count: 3,
    tone: "wiki",
    description: "Наличие статьи, посещаемость, возраст статьи",
  },
  {
    name: "Эмбеддинговые",
    count: 3,
    tone: "embedding",
    description: "Близость к эталонным центроидам ранних и зрелых технологий, близость запросу",
  },
];

const LIMITATIONS = [
  "Отрицательный класс для обучения размечен командой, а не организаторами.",
  "Тексты классов написаны разными авторами: метрики на собственной выборке завышены.",
  "Нарративы формируются только по найденным источникам; без доступа к языковой модели они собираются экстрактивно и помечаются в выдаче.",
  "Система предлагает гипотезы для эксперта и не заменяет экспертную оценку.",
];

export default function MethodologyPage() {
  useEffect(() => {
    document.title = "Методология — Слабые сигналы";
  }, []);

  const featureTotal = FEATURE_GROUPS.reduce((sum, group) => sum + group.count, 0);

  return (
    <div className="page method">
      <header className="method__head">
        <h1 className="page-title">Методология</h1>
        <p className="page-lead">
          Слабый сигнал — технология на ранней стадии: о ней уже пишут в научных и патентных
          источниках, но рынок не сформирован, лидеры не выражены, массового внедрения нет.
          Система отделяет такие технологии от зрелых решений, отраслевых стандартов,
          маркетингового хайпа и информационного шума.
        </p>
      </header>

      <section className="method__block method__block--steps" aria-labelledby="steps-heading">
        <h2 id="steps-heading" className="section-title">
          Как формируется выдача
        </h2>
        <ol className="steps">
          {STEPS.map((step, index) => (
            <li key={step} className="steps__item">
              <span className="steps__number" aria-hidden="true">
                {index + 1}
              </span>
              <p className="steps__text">{step}</p>
            </li>
          ))}
        </ol>
      </section>

      <div className="method__column">
        <section className="method__block" aria-labelledby="trust-heading">
          <h2 id="trust-heading" className="section-title">
            Уровни доверенности источников
          </h2>
          <ul className="trust">
            {TRUST_RULES.map((rule) => (
              <li key={rule.level} className={`trust__item trust__item--${rule.tone}`}>
                <span className="trust__meter" aria-hidden="true">
                  {[1, 2, 3].map((bar) => (
                    <span key={bar} className={bar <= rule.strength ? "trust__bar trust__bar--on" : "trust__bar"} />
                  ))}
                </span>
                <strong className="trust__level">{rule.level}</strong>
                <span className="trust__text">{rule.description}</span>
              </li>
            ))}
          </ul>
          <p className="note">
            Пресс-релизы и блоги используются только как первичный индикатор: кандидат, подтверждённый
            лишь ими, в выдачу не попадает.
          </p>
        </section>

        <section className="method__block" aria-labelledby="rules-heading">
          <h2 id="rules-heading" className="section-title">
            Правила исключения
          </h2>
          <ol className="rules">
            {EXCLUSION_RULES.map(([name, condition], index) => (
              <li key={name} className="rules__item">
                <span className="rules__order" aria-hidden="true">
                  {index + 1}
                </span>
                <strong className="rules__name">{name}</strong>
                <span className="rules__text">{condition}</span>
              </li>
            ))}
          </ol>
          <p className="note">
            Правила применяются до модели и в фиксированном порядке — побеждает первое подходящее.
            Причина исключения показывается на странице результатов.
          </p>
        </section>
      </div>

      <div className="method__column">
        <section className="method__block" aria-labelledby="groups-heading">
          <h2 id="groups-heading" className="section-title">
            Группы признаков
          </h2>
          <div className="groups__bar" aria-hidden="true">
            {FEATURE_GROUPS.map((group) => (
              <span
                key={group.name}
                className={`groups__segment groups__segment--${group.tone}`}
                style={cssVars({ "--weight": group.count })}
              >
                {group.count}
              </span>
            ))}
          </div>
          <ul className="groups">
            {FEATURE_GROUPS.map((group) => (
              <li key={group.name} className={`groups__item groups__item--${group.tone}`}>
                <strong className="groups__name">
                  {group.name} ({group.count})
                </strong>
                <span className="groups__text">{group.description}</span>
              </li>
            ))}
          </ul>
          <p className="note">Всего признаков: {featureTotal}.</p>
        </section>

        <section className="method__block" aria-labelledby="limits-heading">
          <h2 id="limits-heading" className="section-title">
            Ограничения
          </h2>
          <ul className="limits">
            {LIMITATIONS.map((limit) => (
              <li key={limit}>{limit}</li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
