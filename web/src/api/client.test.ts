/**
 * API client 的失败归类契约（照 fd-shilun 同款）。
 *
 * 界面靠这两种异常决定显示什么：
 * - `ApiUnavailableError` → 「后端不可达 / 接线中」
 * - `ApiError`           → 「请求失败（4xx）：<后端 detail>」
 *
 * 归类错了，用户会看到「接线中」而实际是权限问题（或反过来），排障方向全错。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  API_BASE, api, ApiError, ApiUnavailableError, getToken, setToken,
} from "./client";

function mockFetch(impl: (url: string, init?: RequestInit) => Promise<Response> | Response) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setToken("");
});

describe("token", () => {
  it("setToken 后 getToken 取回；空串即清空", () => {
    setToken("abc");
    expect(getToken()).toBe("abc");
    setToken("");
    expect(getToken()).toBe("");
  });
});

describe("请求归类", () => {
  it("200 → 返回解析后的 JSON", async () => {
    mockFetch(() => jsonResponse({ prefs: { statistical: true, means: true, personal: true } }));
    const out = await api.getJudgmentPrefs();
    expect(out.prefs.statistical).toBe(true);
  });

  it("带 token 时发 Authorization", async () => {
    setToken("tok-1");
    let seen: string | undefined;
    mockFetch((_u, init) => {
      seen = (init?.headers as Record<string, string>)?.Authorization;
      return jsonResponse({ user_id: "alice" });
    });
    await api.me();
    expect(seen).toBe("Bearer tok-1");
  });

  it("401 → ApiError（不是 unavailable：这是权限问题，不是接线问题）", async () => {
    mockFetch(() => jsonResponse({ detail: "unauthorized" }, 401));
    await expect(api.getJudgmentPrefs()).rejects.toBeInstanceOf(ApiError);
  });

  it("422 带 detail 对象 → ApiError 携带可读消息", async () => {
    mockFetch(() => jsonResponse({ detail: { reason: "invalid_prefs", detail: "未知判断层" } }, 422));
    await expect(api.setJudgmentPrefs({ statistical: false }))
      .rejects.toThrow(/未知判断层/);
  });

  it("500 → ApiUnavailableError（能力暂不可用）", async () => {
    mockFetch(() => jsonResponse({}, 500));
    await expect(api.me()).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("网络失败（fetch 抛）→ ApiUnavailableError", async () => {
    mockFetch(() => Promise.reject(new TypeError("failed to fetch")));
    await expect(api.me()).rejects.toBeInstanceOf(ApiUnavailableError);
  });
});

describe("端点形状", () => {
  it("impact 以 POST 带 target_kind / target_ref", async () => {
    let body: unknown;
    mockFetch((_u, init) => {
      body = JSON.parse(String(init?.body));
      return jsonResponse({ target: {}, claims: [], judgment: {}, gaps: [], situation_overlap: [] });
    });
    await api.impact("entity", "习近平");
    expect(body).toEqual({ target_kind: "entity", target_ref: "习近平" });
  });

  it("setJudgmentPrefs 是 PUT（部分更新）", async () => {
    let method: string | undefined;
    mockFetch((_u, init) => {
      method = init?.method;
      return jsonResponse({ prefs: { statistical: false, means: true, personal: true } });
    });
    await api.setJudgmentPrefs({ statistical: false });
    expect(method).toBe("PUT");
  });
});

describe("生产可达性（这个如果坏了，前端一个请求都发不出去）", () => {
  it("默认 base 是同源空串，不是 localhost", () => {
    // 生产由 nginx 反代 /api/ → 集群内 fd-zhengxin-api（web/nginx.conf）。
    // 默认写成 http://127.0.0.1:8011 会让浏览器去打用户自己的机器。
    expect(API_BASE).toBe("");
  });

  it("空 base 时请求打到相对路径 /api/...", async () => {
    let url: string | undefined;
    vi.stubGlobal("fetch", vi.fn((u: string) => {
      url = u;
      return Promise.resolve(jsonResponse({ user_id: "x" }));
    }));
    await api.me();
    expect(url).toBe("/api/v1/me");
  });
});
