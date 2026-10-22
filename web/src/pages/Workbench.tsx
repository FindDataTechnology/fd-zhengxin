import { useTranslation } from "react-i18next";

/** 工作台：读 / 问 / 辨 / 轮 四闭环（骨架空页，业务 UI 接线期落）。 */
export default function Workbench() {
  const { t } = useTranslation();
  return (
    <main className="mx-auto max-w-3xl px-6 py-10">
      <h1 className="font-display text-2xl">{t("workbench.title")}</h1>
      <p className="mt-1 text-sm text-neutral-500">{t("workbench.subtitle")}</p>
      <p className="mt-8 rounded border border-dashed border-line-strong bg-white px-6 py-10 text-center text-sm text-neutral-400">
        {t("workbench.placeholder")}
      </p>
    </main>
  );
}
