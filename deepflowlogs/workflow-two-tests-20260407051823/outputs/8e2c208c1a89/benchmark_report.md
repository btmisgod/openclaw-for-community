# neko 外部对标报告

- run_id: 8e2c208c1a89
- benchmark_mode: fallback
- search_query: Russia jails former Kursk governor in Ukraine incursion-linked graft probe international news roundup page

## 对标对象
- BBC News | https://www.bbc.com/news | BBC News - Breaking news, video and the latest top stories from the U.S. and around the world | 被选原因：开放搜索结果不可用，回退到固定参考样本。
- Reuters World | https://www.reuters.com/world/ | 未抓到标题 | 被选原因：开放搜索失败后的固定样本兜底。
- Google News | https://news.google.com/topstories?hl=en-US&gl=US&ceid=US:en | Google News | 被选原因：开放搜索结果不可用，回退到固定参考样本。

## 最明显差距
- BBC News 首页更强调第一屏层级和强标题，我们这轮的主推冲击力与版式完成度还有差距。
- 未能稳定抓取 Reuters World 页面：401 Client Error: HTTP Forbidden for url: https://www.reuters.com/world/
- Google News 首页更强调第一屏层级和强标题，我们这轮的主推冲击力与版式完成度还有差距。

## 可落到下一轮的建议
- 参考 BBC News 的读者入口设计，下轮优先让主推首句更直接、图片更稳定、板块首屏更有层次。
- 保留 Reuters World 作为对标对象，但下轮仍按“首屏层级 + 重点更直接”推进。
- 参考 Google News 的读者入口设计，下轮优先让主推首句更直接、图片更稳定、板块首屏更有层次。