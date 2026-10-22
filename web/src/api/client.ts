/**
 * 极简 API client（骨架，照 fd-zhesi web/src/api/client.ts 的形态裁剪）。
 *
 * - base URL 读 import.meta.env.VITE_ZHENGXIN_API_BASE，默认本地后端 http://127.0.0.1:8011
 * - token 存 localStorage["zhengxin_token"]（顶栏输入框设置，开发态）
 * - 业务端点（读/问/辨/轮）接线期在此扩展；骨架期只有存取与 base。
 */

export const API_BASE: string = (() => {
  const raw = import.meta.env?.VITE_ZHENGXIN_API_BASE;
  return typeof raw === "string" && raw.trim() ? raw.trim().replace(/\/+$/, "") : "http://127.0.0.1:8011";
})();

export const TOKEN_KEY = "zhengxin_token";

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
