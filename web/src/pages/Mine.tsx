import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api, mapApiError, type JudgmentPrefs, type Loadable, type SituationItem,
} from "../api/client";

/**
 * 「我的」页：跨对象的累积视图（理解卡 / 学习地图 / 决策留痕 / 处境 / 判断偏好）。
 *
 * 与 /workbench 分开的理由：材料与判断是**一次查询的产出**，而卡/地图/决策/处境
 * 是**跨对象生长的**。混在一排 tab 里，用户每理解一个新议题都要路过自己的资料库。
 *
 * 本版只落「处境」与「判断偏好」两块（这两块是判断层的前置），
 * 理解卡/地图/决策留痕列表留待后续——它们各自有独立形态，不该塞进这一波。
 */

const SITUATION_KINDS = ["region", "industry", "occupation", "asset_class"] as const;

export default function Mine() {
  const { t } = useTranslation();
  const [prefs, setPrefs] = useState<Loadable<JudgmentPrefs>>({ status: "loading" });
  const [situation, setSituation] = useState<Loadable<SituationItem[]>>({ status: "loading" });
  const [draftKind, setDraftKind] = useState<string>("industry");
  const [draftValue, setDraftValue] = useState("");

  const load = useCallback(async () => {
    try {
      setPrefs({ status: "ready", data: (await api.getJudgmentPrefs()).prefs });
    } catch (e) {
      setPrefs(mapApiError(e));
    }
    try {
      setSituation({ status: "ready", data: (await api.listSituation()).situation });
    } catch (e) {
      setSituation(mapApiError(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (key: keyof JudgmentPrefs) => {
    if (prefs.status !== "ready") return;
    const next = { ...prefs.data, [key]: !prefs.data[key] };
    setPrefs({ status: "ready", data: next });
    try {
      const out = await api.setJudgmentPrefs({ [key]: next[key] });
      setPrefs({ status: "ready", data: out.prefs });
    } catch (e) {
      setPrefs(mapApiError(e));
    }
  };

  const addSituation = async () => {
    if (!draftValue.trim()) return;
    try {
      await api.declareSituation(draftKind, draftValue.trim());
      setDraftValue("");
      await load();
    } catch (e) {
      setSituation(mapApiError(e));
    }
  };

  return (
    <main className="mx-auto max-w-3xl px-6 py-10">
      <h1 className="font-display text-2xl">{t("mine.title")}</h1>
      <p className="mt-1 text-sm text-neutral-500">{t("mine.subtitle")}</p>

      {/* 判断偏好 */}
      <section className="mt-8">
        <h2 className="text-sm font-medium text-neutral-500">{t("mine.prefs")}</h2>
        <p className="mt-1 text-xs text-neutral-400">{t("mine.prefsNote")}</p>
        {prefs.status === "ready" ? (
          <ul className="mt-3 space-y-2">
            {(["statistical", "means", "personal"] as const).map((k) => (
              <li key={k} className="flex items-center gap-3 rounded border border-neutral-200 bg-white px-3 py-2">
                <input
                  id={`pref-${k}`}
                  type="checkbox"
                  checked={prefs.data[k]}
                  onChange={() => void toggle(k)}
                  className="h-4 w-4"
                />
                <label htmlFor={`pref-${k}`} className="flex-1 text-sm">
                  {t(`judgment.layer.${k}`)}
                </label>
                <span className="text-xs text-neutral-400">
                  {prefs.data[k] ? t("mine.on") : t("mine.off")}
                </span>
              </li>
            ))}
          </ul>
        ) : prefs.status === "loading" ? (
          <p className="mt-2 text-xs text-neutral-400">{t("mine.loading")}</p>
        ) : (
          <p className="mt-2 text-xs text-neutral-500">{prefs.message}</p>
        )}
      </section>

      {/* 处境档案 */}
      <section className="mt-10">
        <h2 className="text-sm font-medium text-neutral-500">{t("mine.situation")}</h2>
        <p className="mt-1 text-xs text-neutral-400">{t("mine.situationNote")}</p>

        <div className="mt-3 flex gap-2">
          <select
            value={draftKind}
            onChange={(e) => setDraftKind(e.target.value)}
            aria-label={t("mine.kindLabel")}
            className="rounded border border-neutral-300 px-2 py-2 text-sm"
          >
            {SITUATION_KINDS.map((k) => (
              <option key={k} value={k}>{t(`mine.kind.${k}`)}</option>
            ))}
          </select>
          <input
            value={draftValue}
            onChange={(e) => setDraftValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void addSituation()}
            placeholder={t("mine.valuePlaceholder")}
            aria-label={t("mine.valuePlaceholder")}
            className="flex-1 rounded border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
          <button
            type="button"
            onClick={() => void addSituation()}
            className="rounded bg-neutral-900 px-3 py-2 text-sm text-white hover:bg-neutral-800"
          >
            {t("mine.add")}
          </button>
        </div>

        {situation.status === "ready" ? (
          situation.data.length ? (
            <ul className="mt-3 space-y-1">
              {situation.data.map((s) => (
                <li key={s.id} className="flex items-center gap-2 text-sm">
                  <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-500">
                    {t(`mine.kind.${s.kind}`, s.kind)}
                  </span>
                  <span>{s.value}</span>
                  <span className="ml-auto text-xs text-neutral-400">{s.expires_at.slice(0, 10)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-3 text-xs text-neutral-400">{t("mine.situationEmpty")}</p>
          )
        ) : situation.status === "loading" ? null : (
          <p className="mt-2 text-xs text-neutral-500">{situation.message}</p>
        )}
      </section>
    </main>
  );
}