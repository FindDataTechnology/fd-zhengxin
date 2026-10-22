# deploy

照 fd 舰队部署配方（cheap fd-prod + TCR + ArgoCD），形制照 fd-zhesi。
web（fd-zhengxin-web）与 api（fd-zhengxin-api）两镜像；tag 随 CI 出片（.github/workflows/image.yml）。

参照系服务独立部署、独立库（地址 `FDZX__REFERENCE_BASE_URL`，见 server/fd_zhengxin_api/config.py），
不经本目录清单管理——照 fd-zhesi / fd-zhesi-reference 分仓先例。

骨架期清单为占位形态：镜像 tag 以 CI 首次出片为准，接入期再落雷池/证书（SAFELINE-CERTS.md）。
