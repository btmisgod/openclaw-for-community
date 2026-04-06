# 配置差异说明

- /opt/newsflow-mvp/app/: 联调期间新增/修改 tracing、渲染与归档支撑代码。
- newsflow-dashboard.json: 拆分 Agent Communication Feed、Review & Reject Thread、Final Report Preview。
- systemd 服务定义: 维持 orchestrator 和 3 个 agent 的常驻运行方式。
- deepflow-grafana nginx 配置: 增加 /newsflow/ 路由用于动态会话页和成品页。
- /opt/deepflow-deploy/.env: 修复 STANDALONE 节点地址，避免同机 agent 回传到公网 IP。
- grafana.ini: 开启 disable_sanitize_html，允许 dashboard 内嵌 HTML 预览。
- deepflow-agent 配置: 保留实验 deepflow-agent 与 OTLP 接入相关配置。