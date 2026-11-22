import { useTranslation } from "react-i18next";
import type { JudgmentItem, JudgmentLayer } from "../../api/client";

/**
 * 判断信封卡片。
 *
 * 六个字段缺一不可——缺了后端本来就不会产出（judgment.validate_judgment 是准入
 * 校验，不是风格建议）。这里遇到缺字段的输入**拒绝渲染**并抛出，而不是补默认值：
 * 补默认值会让一条不合格的判断看起来合格，那是这个组件唯一不能做的事。
 */

const CONFIDENCE_STYLE: Record<string, string> = {
  high: "bg-pine/10 text-pine",
  medium: "bg-amber-50 text-amber-800",
  low: "bg-neutral-100 text-neutral-600",
};

export function JudgmentCard({ item }: { item: JudgmentItem }) {
  const { t } = useTranslation();

  for (const k of ["conclusion", "may_be_wrong", "would_overturn"] as const) {
    if (!item[k]?.trim()) {
      throw new Error(`判断信封缺 ${k}：不合格的判断不得渲染`);
    }
  }
  if (!item.evidence?.length) {
    throw new Error("判断信封缺依据链：没有依据的判断是断言");
  }

  return (
    <article className="rounded-md border border-neutral-200 bg-white p-4">
      <div className="flex items-start gap-3">
        <p className="flex-1 leading-relaxed text-neutral-900">{item.conclusion}</p>
        <span
          className={`shrink-0 rounded px-2 py-0.5 text-xs ${
            CONFIDENCE_STYLE[item.confidence] ?? CONFIDENCE_STYLE.low
          }`}
        >
          {t(`judgment.confidence.${item.confidence}`, item.confidence)}
        </span>
      </div>

      <div className="mt-3 border-t border-neutral-100 pt-3">
        <p className="text-xs font-medium text-neutral-500">{t("judgment.evidence")}</p>
        <ul className="mt-1 space-y-1">
          {item.evidence.map((e, i) => (
            <li key={`${e.ref}-${i}`} className="text-xs leading-relaxed text-neutral-600">
              <code className="rounded bg-neutral-100 px-1">{e.ref}</code>
              {e.quote ? <span className="ml-2">「{e.quote}」</span> : null}
              {e.note ? <span className="ml-2">{e.note}</span> : null}
            </li>
          ))}
        </ul>
      </div>

      <dl className="mt-3 space-y-2 border-t border-neutral-100 pt-3 text-xs leading-relaxed">
        <div>
          <dt className="font-medium text-neutral-500">{t("judgment.mayBeWrong")}</dt>
          <dd className="mt-0.5 text-neutral-700">{item.may_be_wrong}</dd>
        </div>
        <div>
          <dt className="font-medium text-neutral-500">{t("judgment.wouldOverturn")}</dt>
          <dd className="mt-0.5 text-neutral-700">{item.would_overturn}</dd>
        </div>
      </dl>
    </article>
  );
}

/** 一层判断：ok 渲染卡片；disabled / unavailable 各显示一行原因，不给灰版本。 */
export function JudgmentLayerBlock({ name, layer }: { name: string; layer: JudgmentLayer }) {
  const { t } = useTranslation();

  // 关闭 = 用户不想要，显示「已关闭」；不可用 = 能力缺失，显示缺什么。
  // 两者不可混（混了会让人以为是 bug）。
  if (layer.status === "disabled") {
    return (
      <div className="rounded border border-dashed border-neutral-200 px-3 py-2 text-xs text-neutral-400">
        {t(`judgment.layer.${name}`)} · {t("judgment.disabled")}
      </div>
    );
  }
  if (layer.status === "unavailable" || layer.status === "error") {
    return (
      <div className="rounded border border-dashed border-neutral-200 px-3 py-2 text-xs text-neutral-500">
        {t(`judgment.layer.${name}`)} · {t("judgment.unavailable")}
        {layer.why ? <span className="ml-1 text-neutral-400">（{layer.why}）</span> : null}
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <p className="text-xs font-medium text-neutral-500">{t(`judgment.layer.${name}`)}</p>
      {layer.items.map((it, i) => (
        <JudgmentCard key={i} item={it} />
      ))}
    </div>
  );
}
