# 政心（zhengxin）公网接入：SafeLine + 证书配置单（骨架占位）

> 形制照 fd-zhesi/deploy/SAFELINE-CERTS.md；服务尚未部署，本文为接入期模板，
> 实际域名 / NodePort / 回源地址以接入当天的集群现状为准，不预写死值。

## 1. 需要暴露什么（只有一个站点要建）

| 项 | 值（接入期填） |
|---|---|
| 建议域名 | `zhengxin.finddatatech.cloud`（与 lex./zhesi. 同族；有 `*.finddatatech.cloud` 泛域名证书可直接用） |
| 回源地址 | `http://<cheap 节点>:31890`（fd-zhengxin-web NodePort，deploy/k8s 占位值） |
| 回源协议 | HTTP（集群内无 TLS），对外 HTTPS + HTTP→HTTPS 跳转 |
| 说明 | **只需要这一个站点**。API（fd-zhengxin-api）不单独暴露——web 的 nginx 已把 `/api/` 反代到集群内 api 服务；参照系服务走内网/网关注册通道，不直接上公网 |

## 2. 站点行为要点

- **上游健康检查**：`GET /healthz` 期待 200（nginx 直接回 `ok`，无依赖）。
- **不要缓存 `index.html`**：SPA（React），发版后必须拿到新 hash 的资源文件名；js/css 带 7 天缓存指纹无妨。
- **超时**：`/api/` 有 LLM 调用时回源读超时请 ≥ 60s（web→api 反代已设 60s，雷池侧别低于它）。

## 3. 雷池已知坑（家族实测，务必处理）

1. **拦截含代码/脚本片段的 POST body**：`/api/v1/*` 的 JSON 请求体里可能含原典引文
   （引号/连字符密集）。请对路径 `^/api/` 的 POST 做放行（白名单规则）或把该站点
   body 检测降级，否则读/问闭环会被误拦。
2. **Bearer 头**：所有 `/api/` 调用带 `Authorization: Bearer <JWT>`，请确认雷池不剥这个头。
3. **SPA 强制重载**（wire 四屏的教训）：页面出不来先硬刷新再排障，避免把缓存问题误判成服务问题。

## 4. 证书

- 单域名或复用泛域名均可；证书装在雷池侧即可，集群内全部 HTTP 回源，无需改任何 manifest。

## 5. 接入后验收清单（按序 curl）

```bash
curl -s https://zhengxin.finddatatech.cloud/healthz        # -> ok
curl -s https://zhengxin.finddatatech.cloud/ | grep -o '<title>.*</title>'   # -> 政心 Zhengxin
# 鉴权边界（应 401，证明 /api/ 反代与 Bearer 头未被剥）
curl -s -o /dev/null -w '%{http_code}\n' https://zhengxin.finddatatech.cloud/api/v1/me
```
