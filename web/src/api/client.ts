/**
 * 政心 API client（原生 fetch，零第三方依赖；形制照 ../fd-shilun/web/src/api/client.ts）。
 *
 * - base URL 读 `VITE_ZHENGXIN_API_BASE`，**默认空串 = 同源**。
 *   生产由 nginx 反代 `/api/` 到集群内 fd-zhengxin-api（见 web/nginx.conf），
 *   所以默认必须是相对路径；写死 127.0.0.1:8011 会让生产前端一个请求都发不出去。
 *   本地联调用 `VITE_ZHENGXIN_API_BASE=http://127.0.0.1:8011 npm run dev` 覆盖。
 * - token 存 `localStorage["zhengxin_token"]`（顶栏输入框设置，开发态）
 * - 网络失败 / 超时 / 5xx → `ApiUnavailableError`（界面据此显示「接线中」降级）
 * - 4xx → `ApiError`（带后端 detail）
 *
 * 只有「工作台实际用到」的端点在这里，不预先抄全量——没调用的方法会腐。
 */

export const API_BASE: string = (() => {
  const raw = import.meta.env?.VITE_ZHENGXIN_API_BASE;
  return typeof raw === "string" && raw.trim() ? raw.trim().replace(/\/+$/, "") : "";
})();

export const TOKEN_KEY = "zhengxin_token";
const TIMEOUT_MS = 60_000;

// ---- token 存取（非浏览器环境退化为内存 shim，测试可用） ----

const memoryStore = new Map<string, string>();
const shim: Pick<Storage, "getItem" | "setItem" | "removeItem"> = {
  getItem: (k) => memoryStore.get(k) ?? null,
  setItem: (k, v) => void memoryStore.set(k, v),
  removeItem: (k) => void memoryStore.delete(k),
};
function store(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch {
    /* 隐私模式等场景落到内存 shim */
  }
  return shim;
}
export function getToken(): string {
  return store().getItem(TOKEN_KEY) ?? "";
}
export function setToken(token: string): void {
  if (token) store().setItem(TOKEN_KEY, token);
  else store().removeItem(TOKEN_KEY);
}

// ---- 错误类型 ----

/** 网络失败 / 超时 / 5xx：能力暂不可用（不是用户操作错误）。 */
export class ApiUnavailableError extends Error {
  constructor(message = "backend unreachable") {
    super(message);
    this.name = "ApiUnavailableError";
  }
}

/** 业务错误（4xx），message 携带后端 detail。 */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// ---- 请求 ----

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
}

function buildUrl(path: string): string {
  return `${API_BASE}${path}`;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const hasBody = opts.body !== undefined;
  if (hasBody) headers["Content-Type"] = "application/json";

  let resp: Response;
  try {
    resp = await fetch(buildUrl(path), {
      method: opts.method ?? (hasBody ? "POST" : "GET"),
      headers,
      body: hasBody ? JSON.stringify(opts.body) : undefined,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    throw new ApiUnavailableError();
  }

  if (!resp.ok) {
    if (resp.status >= 500) throw new ApiUnavailableError();
    let detail: unknown = "";
    try {
      detail = (await resp.json())?.detail ?? "";
    } catch {
      /* 无 JSON body */
    }
    const msg = typeof detail === "string" ? detail
      : (detail as { detail?: string })?.detail ?? JSON.stringify(detail);
    throw new ApiError(resp.status, msg || resp.statusText);
  }
  return (await resp.json()) as T;
}

// ---- 域类型（只覆盖工作台用到的字段） ----

/** 判断信封（六字段缺一不可——缺了后端就不会产出，见 judgment.py 的准入校验）。 */
export interface JudgmentItem {
  conclusion: string;
  evidence: { ref: string; quote?: string; note?: string }[];
  confidence: "high" | "medium" | "low";
  may_be_wrong: string;
  would_overturn: string;
  layer: string;
  sample_size?: number;
}

export interface JudgmentLayer {
  status: "ok" | "disabled" | "unavailable" | "error";
  items: JudgmentItem[];
  why?: string;
  situation?: "declared" | "absent";
}

export interface JudgmentBlock {
  enabled: boolean;
  layers: { statistical: JudgmentLayer; means: JudgmentLayer; personal: JudgmentLayer };
}

export interface JudgmentPrefs {
  statistical: boolean;
  means: boolean;
  personal: boolean;
}

export interface ImpactResponse {
  target: { kind: string; ref: string };
  match_mode: string;
  coverage: { status: string; detail?: string };
  claims: {
    item_id: string; title: string; published_at?: string;
    body_available: boolean;
    elements: { element: string; value: string; direction?: string; quote?: string }[];
  }[];
  situation_overlap: { element: string; relevance: string; detail: string }[];
  judgment: JudgmentBlock;
  gaps: { stage: string; reason?: string; detail?: string }[];
  notice: string;
}

export interface SituationItem {
  id: string; kind: string; value: string;
  declared_at: string; expires_at: string; revoked_at: string | null;
}

// ---- 端点 ----

export const api = {
  me: () => request<{ user_id: string }>("/api/v1/me"),

  // 影响面环。四种来源：
  //   url     一个网页/PDF 链接 —— 用户手里就有的东西
  //   file    上传的 PDF / 文本（走 /impact/file，multipart）
  //   entity  人物/事件名
  //   claim   既有条目 id（保留旧路径）
  impact: (targetKind: "claim" | "entity" | "url", targetRef: string) =>
    request<ImpactResponse>("/api/v1/loops/impact", {
      method: "POST",
      body: { target_kind: targetKind, target_ref: targetRef },
    }),

  impactFile: async (file: File): Promise<ImpactResponse> => {
    const token = getToken();
    const form = new FormData();
    form.append("file", file);
    form.append("is_contemporary", "true");
    let resp: Response;
    try {
      resp = await fetch(buildUrl("/api/v1/loops/impact/file"), {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
    } catch {
      throw new ApiUnavailableError();
    }
    if (!resp.ok) {
      if (resp.status >= 500) throw new ApiUnavailableError();
      let detail: unknown = "";
      try { detail = (await resp.json())?.detail ?? ""; } catch { /* no body */ }
      throw new ApiError(resp.status, typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return (await resp.json()) as ImpactResponse;
  },

  // 判断偏好（默认全开；无行即全开）
  getJudgmentPrefs: () =>
    request<{ prefs: JudgmentPrefs }>("/api/v1/judgment/prefs"),
  setJudgmentPrefs: (prefs: Partial<JudgmentPrefs>) =>
    request<{ prefs: JudgmentPrefs }>("/api/v1/judgment/prefs", {
      method: "PUT", body: prefs,
    }),

  // 处境档案（经济四轴；可导出可清空）
  listSituation: () => request<{ situation: SituationItem[] }>("/api/v1/situation"),
  declareSituation: (kind: string, value: string) =>
    request<{ situation: SituationItem }>("/api/v1/situation", {
      method: "POST", body: { kind, value },
    }),
  clearSituation: () => request<{ deleted: number }>("/api/v1/situation", { method: "DELETE" }),
};

/** 统一把异常映射成界面可渲染的状态（照 fd-shilun 的 mapApiError）。 */
export type Loadable<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "unavailable"; message: string }
  | { status: "error"; message: string };

export function mapApiError(e: unknown): Loadable<never> {
  if (e instanceof ApiUnavailableError) {
    return { status: "unavailable", message: "后端不可达 —— 该能力接线中" };
  }
  if (e instanceof ApiError) {
    return { status: "error", message: `请求失败（${e.status}）：${e.message}` };
  }
  return { status: "error", message: e instanceof Error ? e.message : String(e) };
}
