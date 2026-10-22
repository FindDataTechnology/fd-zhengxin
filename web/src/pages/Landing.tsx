import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

function LoopBand({ index, name, title, body, detail }: { index: number; name: string; title: string; body: string; detail: string }) {
  return (
    <section className={`border-t border-line ${index % 2 === 1 ? "bg-mist" : "bg-paper"}`}>
      <div className="mx-auto grid max-w-5xl gap-6 px-6 py-14 md:grid-cols-[10rem_1fr] md:gap-12 md:py-16">
        <div className="font-display text-2xl text-pine md:text-right">{name}</div>
        <div className="max-w-2xl">
          <h2 className="font-display text-2xl leading-snug text-ink md:text-[1.7rem]">{title}</h2>
          <p className="mt-4 leading-loose text-ink-soft">{body}</p>
          <p className="mt-3 text-sm leading-relaxed text-ink-soft/90">{detail}</p>
        </div>
      </div>
    </section>
  );
}

export default function Landing() {
  const { t } = useTranslation();
  const loops = (["read", "ask", "discern", "rings"] as const).map((k) => ({
    name: t(`landing.loops.${k}.name`),
    title: t(`landing.loops.${k}.title`),
    body: t(`landing.loops.${k}.body`),
    detail: t(`landing.loops.${k}.detail`),
  }));
  return (
    <main className="bg-paper">
      {/* Hero——安静的书斋 */}
      <section className="bg-night text-moon">
        <div className="mx-auto max-w-6xl px-6 py-20 lg:py-28">
          <div className="max-w-3xl">
            <h1 className="hero-rise font-display text-5xl leading-tight tracking-wide text-white md:text-6xl">
              {t("landing.hero.statement1")}
            </h1>
            <p className="hero-rise hero-rise-1 mt-5 font-display text-2xl leading-snug text-moon/85 md:text-3xl">
              {t("landing.hero.statement2")}
            </p>
            <p className="hero-rise hero-rise-2 mt-7 max-w-xl leading-loose text-moon/70">
              {t("landing.hero.lede")}
            </p>
            <div className="hero-rise hero-rise-3 mt-10 flex flex-wrap items-center gap-4">
              <Link
                to="/workbench"
                className="rounded-md bg-moon px-6 py-3 text-sm font-medium text-night-deep transition-colors hover:bg-white"
              >
                {t("landing.hero.cta")}
              </Link>
              <a href="#loops" className="rounded-md border border-moon/30 px-6 py-3 text-sm text-moon transition-colors hover:border-moon/70">
                {t("landing.hero.ctaSecondary")}
              </a>
            </div>
            <p className="hero-rise hero-rise-3 mt-8 text-xs leading-relaxed text-moon/45">
              {t("landing.hero.sources")}
            </p>
          </div>
        </div>
      </section>

      {/* 四个闭环——编辑部式横带，非卡片阵 */}
      <div id="loops" className="scroll-mt-16">
        {loops.map((l, i) => (
          <LoopBand key={l.name} index={i} {...l} />
        ))}
      </div>

      {/* 口径纪律——产品真话 */}
      <section className="border-t border-line bg-night-deep text-moon">
        <div className="mx-auto max-w-3xl px-6 py-16 text-center">
          <h2 className="font-display text-2xl leading-snug text-white md:text-3xl">{t("landing.discipline.title")}</h2>
          <p className="mx-auto mt-5 max-w-xl leading-loose text-moon/70">{t("landing.discipline.body")}</p>
        </div>
      </section>

      {/* CTA */}
      <section className="border-t border-line bg-paper">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center">
          <h2 className="font-display text-3xl leading-snug text-ink">{t("landing.cta.title")}</h2>
          <p className="mt-4 leading-loose text-ink-soft">{t("landing.cta.body")}</p>
          <Link
            to="/workbench"
            className="mt-8 inline-block rounded-md bg-night px-8 py-3.5 text-sm font-medium text-moon transition-colors hover:bg-night-deep"
          >
            {t("landing.hero.cta")}
          </Link>
          <p className="mt-6 text-xs text-ink-soft/70">{t("landing.cta.family")}</p>
        </div>
      </section>
    </main>
  );
}
