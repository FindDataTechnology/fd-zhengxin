/**
 * 三入口渲染哨兵（spec `web-shell-kit`「每宿主须有哨兵」）。
 *
 * fd-zhengxin 的壳承载三个入口：`/`（Landing）、`/workbench`、`/admin`。
 * 「入口可见」在这里落成一句可判的话：**该入口独有的文案出现在渲染输出里，且两语都成立**——
 * 壳升级把文案键改错、某个入口渲不出来、或者 i18n 的键被裁掉，这里就红。
 *
 * 无 jsdom：用 `react-dom/server` 的 `renderToString` 直渲视图层，
 * 与 fd-zhesi / fd-shilun 的 `Workbench.test.tsx` 同法。
 *
 * 2026-11-14 建：此前这个宿主 `package.json` 没有 `test` 脚本、一个测试文件都没有，
 * 家族哨兵只能把它登记成「已登记未覆盖」。本条落地后该登记必须删除。
 */
import { renderToString } from "react-dom/server";
import { readFileSync } from "node:fs";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterAll, describe, expect, it } from "vitest";
import i18n from "../i18n";
import { getToken, setToken } from "../api/client";
import Landing from "./Landing";
import Workbench from "./Workbench";
import Mine from "./Mine";
import Admin from "./Admin";

/** 每个入口一句独有文案（取自 locales，两语各一份）。 */
const ENTRIES = [
  {
    path: "/",
    label: "Landing",
    render: () => <Landing />,
    zh: "把政治思想史，",
    en: "Make the history of political thought",
  },
  {
    path: "/workbench",
    label: "Workbench",
    render: () => <Workbench />,
    zh: "说出你想理解什么",
    en: "Say what you want to understand",
  },
  {
    path: "/workbench/mine",
    label: "Mine",
    render: () => <Mine />,
    zh: "跨议题累积下来的东西",
    en: "What accumulates across topics",
  },
  {
    path: "/admin",
    label: "Admin",
    render: () => <Admin />,
    zh: "平台后台骨架",
    en: "Platform admin skeleton",
  },
] as const;

/** 三个入口都要在路由上下文里渲（Landing/nav 用 `<Link>`）。 */
function renderAt(path: string, node: ReactElement): string {
  return renderToString(<MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>);
}

afterAll(() => {
  void i18n.changeLanguage("zh");
});

describe("三入口渲染哨兵", () => {
  it("App 的 Routes 仍然挂着这三个入口（页面在、路由被删也是壳级事故）", () => {
    const src = readFileSync(new URL("../App.tsx", import.meta.url), "utf8");
    for (const e of ENTRIES) {
      expect(src, `${e.label} 的路由 path="${e.path}" 应出现在 App.tsx 里`).toContain(
        `path="${e.path}"`,
      );
    }
  });

  for (const entry of ENTRIES) {
    it(`${entry.label}（${entry.path}）中文入口可见`, async () => {
      await i18n.changeLanguage("zh");
      const html = renderAt(entry.path, entry.render());
      expect(html, `${entry.label} 应渲染出中文独有文案`).toContain(entry.zh);
    });

    it(`${entry.label}（${entry.path}）英文入口可见`, async () => {
      await i18n.changeLanguage("en");
      const html = renderAt(entry.path, entry.render());
      expect(html, `${entry.label} 应渲染出英文独有文案`).toContain(entry.en);
    });
  }
});

describe("令牌存取（壳的门面）", () => {
  it("setToken 后 getToken 能取回，setToken('') 即清空", () => {
    setToken("smoke-token");
    expect(getToken()).toBe("smoke-token");
    setToken("");
    expect(getToken()).toBe("");
  });
});

describe("i18n 键形状（浏览器走查撞到过）", () => {
  /**
   * `t("mine.kind")` 返回的是**对象**（下面有 region/industry/…），
   * 当字符串用（比如塞进 aria-label）会渲染成
   * "key 'mine.kind (zh)' returned an object instead of string."
   * ——页面不崩，但用户看到一行报错。单测渲染不失败，只有看截图才发现。
   */
  it("Mine 渲染后不出现 i18n 的形状错误文案", async () => {
    await i18n.changeLanguage("zh");
    const html = renderAt("/workbench/mine", <Mine />);
    expect(html).not.toContain("returned an object instead of string");
  });

  it("所有 aria-label 都不是对象型 i18n 键", () => {
    const src = readFileSync(new URL("./Mine.tsx", import.meta.url), "utf8");
    // 「直接 t("x.y") 当 aria-label」这种写法一律可疑：写死成具体文案键才安全
    const bad = [...src.matchAll(/aria-label=\{t\("([^"]+)"\)\}/g)]
      .map((m) => m[1])
      .filter((k) => k === "mine.kind");
    expect(bad).toEqual([]);
  });
});
