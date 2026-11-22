import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, mapApiError, type ImpactResponse, type Loadable } from "../api/client";
import { JudgmentLayerBlock } from "./workbench/JudgmentCard";

/**
 * 工作台：对象为干线。
 *
 * 用户只做一件事——说出想理解什么；材料层与判断层顺着这条线展开。
 * 判断层按开关，关掉的层显示「已关闭」，**不给灰版本**：灰是「系统觉得有问题
 * 但放行」，用户开关是「我压根不想要」，两者不是一回事。
 */

type Mode = "claim" | "entity";

export default function Workbench() {
  const { t } = useTranslation();
  const [mode, setMode] = useState<Mode>("claim");
  const [ref, setRef] = useState("");
  const [result, setResult] = useState<Loadable<ImpactResponse> | null>(null);

  const run = async () => {
    if (!ref.trim()) return;
    setResult({ status: "loading" });
    try {
      setResult({ status: "ready", data: await api.impact(mode, ref.trim()) });
    } catch (e) {
      setResult(mapApiError(e));
    }
  };

  return (
    <main className="mx-auto max-w-3xl px-6 py-10">
      <h1 className="font-display text-2xl">{t("workbench.title")}</h1>
      <p className="mt-1 text-sm text-neutral-500">{t("workbench.subtitle")}</p>

      {/* 对象输入：一个说出「想理解什么」的地方 */}
      <div className="mt-6 rounded-md border border-neutral-200 bg-white p-4">
        <div className="flex gap-1 text-xs">
          {(["claim", "entity"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              aria-pressed={mode === m}
              className={`rounded px-2 py-1 ${
                mode === m ? "bg-neutral-900 text-white" : "text-neutral-500 hover:bg-neutral-100"
              }`}
            >
              {t(`workbench.mode.${m}`)}
            </button>
          ))}
        </div>
        <div className="mt-2 flex gap-2">
          <input
            value={ref}
            onChange={(e) => setRef(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void run()}
            placeholder={t(`workbench.placeholder.${mode}`)}
            aria-label={t(`workbench.placeholder.${mode}`)}
            className="flex-1 rounded border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
          <button
            type="button"
            onClick={() => void run()}
            className="rounded bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-800"
          >
            {t("workbench.go")}
          </button>
        </div>
      </div>

      {result?.status === "loading" ? (
        <p className="mt-8 text-sm text-neutral-400">{t("workbench.loading")}</p>
      ) : null}
      {result?.status === "unavailable" || result?.status === "error" ? (
        <p className="mt-8 rounded border border-dashed border-neutral-300 px-4 py-6 text-center text-sm text-neutral-500">
          {result.message}
        </p>
      ) : null}

      {result?.status === "ready" ? <ResultView data={result.data} /> : null}
    </main>
  );
}

function ResultView({ data }: { data: ImpactResponse }) {
  const { t } = useTranslation();
  const layers = data.judgment?.layers;

  return (
    <div className="mt-8 space-y-8">
      {/* 材料层（常驻） */}
      <section>
        <h2 className="text-sm font-medium text-neutral-500">{t("workbench.material")}</h2>
        <div className="mt-2 space-y-2">
          {data.claims.map((c) => (
            <div key={c.item_id} className="rounded border border-neutral-200 bg-white p-3">
              <p className="text-sm font-medium">{c.title}</p>
              {!c.body_available ? (
                <p className="mt-1 text-xs text-neutral-400">{t("workbench.bodyUnavailable")}</p>
              ) : null}
              {c.elements.length ? (
                <ul className="mt-2 space-y-1">
                  {c.elements.map((el, i) => (
                    <li key={i} className="text-xs text-neutral-600">
                      <span className="font-medium">{el.element}</span>：{el.value}
                      {el.direction ? (
                        <span className="ml-1 text-neutral-400">
                          （{t(`judgment.direction.${el.direction}`, el.direction)}）
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ))}
          {data.claims.length === 0 ? (
            <p className="text-xs text-neutral-400">{t("workbench.noClaims")}</p>
          ) : null}
        </div>
      </section>

      {/* 判断层（按开关） */}
      {layers ? (
        <section>
          <h2 className="text-sm font-medium text-neutral-500">{t("judgment.title")}</h2>
          <div className="mt-2 space-y-3">
            <JudgmentLayerBlock name="statistical" layer={layers.statistical} />
            <JudgmentLayerBlock name="means" layer={layers.means} />
            <JudgmentLayerBlock name="personal" layer={layers.personal} />
          </div>
        </section>
      ) : null}

      {data.gaps.length ? (
        <section>
          <h2 className="text-sm font-medium text-neutral-500">{t("workbench.gaps")}</h2>
          <ul className="mt-2 space-y-1 text-xs text-neutral-500">
            {data.gaps.map((g, i) => (
              <li key={i}>
                <span className="text-neutral-400">{g.stage}</span> {g.detail ?? g.reason}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <p className="rounded bg-neutral-50 p-3 text-xs leading-relaxed text-neutral-500">
        {data.notice}
      </p>
    </div>
  );
}