/**
 * 判断信封卡片的契约：六字段缺一不得渲染。
 *
 * 这不是样式测试。`JudgmentCard` 遇到缺 `may_be_wrong` / `would_overturn` /
 * 依据链的输入会**抛错**而不是补默认值——补默认值会让一条不合格的判断看起来合格，
 * 那是这个组件唯一不能做的事（后端 judgment.validate_judgment 是同一道门的数据面）。
 */
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import i18n from "../../i18n";
import { JudgmentCard, JudgmentLayerBlock } from "./JudgmentCard";
import type { JudgmentItem, JudgmentLayer } from "../../api/client";

const GOOD: JudgmentItem = {
  conclusion: "这份文件对某类主体是收紧方向。",
  evidence: [{ ref: "item:i1", quote: "禁止…" }],
  confidence: "medium",
  may_be_wrong: "字面同义不等于同对象",
  would_overturn: "另有条款点名相反方向",
  layer: "personal",
};

async function render(node: () => ReactElement): Promise<string> {
  await i18n.changeLanguage("zh");
  return renderToString(node());
}

describe("JudgmentCard", () => {
  it("六字段齐全时全部渲染出来", async () => {
    const html = await render(() => <JudgmentCard item={GOOD} />);
    expect(html).toContain(GOOD.conclusion);
    expect(html).toContain(GOOD.may_be_wrong);
    expect(html).toContain(GOOD.would_overturn);
    expect(html).toContain("item:i1");
    expect(html).toContain("禁止…");
  });

  it("缺「我可能错在哪」时拒绝渲染", async () => {
    expect(() => renderToString(<JudgmentCard item={{ ...GOOD, may_be_wrong: "" }} />))
      .toThrow(/may_be_wrong/);
  });

  it("缺「什么会推翻它」时拒绝渲染", async () => {
    expect(() => renderToString(<JudgmentCard item={{ ...GOOD, would_overturn: "  " }} />))
      .toThrow(/would_overturn/);
  });

  it("缺依据链时拒绝渲染", async () => {
    expect(() => renderToString(<JudgmentCard item={{ ...GOOD, evidence: [] }} />))
      .toThrow(/依据链/);
  });
});

describe("JudgmentLayerBlock 状态三态", () => {
  const okLayer: JudgmentLayer = { status: "ok", items: [GOOD] };

  it("ok 渲染卡片", async () => {
    const html = await render(() => <JudgmentLayerBlock name="personal" layer={okLayer} />);
    expect(html).toContain(GOOD.conclusion);
  });

  it("disabled 显示「已关闭」，不给灰版本", async () => {
    const html = await render(
      () => <JudgmentLayerBlock name="statistical" layer={{ status: "disabled", items: [] }} />,
    );
    expect(html).toContain("已关闭");
    expect(html).not.toContain(GOOD.conclusion);
  });

  it("unavailable 显示「暂不可用」并带原因", async () => {
    const html = await render(
      () => <JudgmentLayerBlock name="statistical"
                                layer={{ status: "unavailable", items: [], why: "未接入数据序列" }} />,
    );
    expect(html).toContain("暂不可用");
    expect(html).toContain("未接入数据序列");
  });

  it("disabled 与 unavailable 的文案不同（用户选择 ≠ 能力缺失）", async () => {
    const a = await render(
      () => <JudgmentLayerBlock name="means" layer={{ status: "disabled", items: [] }} />);
    const b = await render(
      () => <JudgmentLayerBlock name="means" layer={{ status: "unavailable", items: [] }} />);
    expect(a).not.toBe(b);
  });
});
