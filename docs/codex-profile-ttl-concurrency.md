# 抖音达人监控：profile 统计字段刷新策略 + 与作品采集并发 —— 征询方案

> 给 Codex 的问题描述。可直接转发。本文所有代码位置均已核实，便于你直接定位与给方案。

## 1. 背景

项目 `D:\JR_project\douyin_creator_monitor` 监控抖音达人主页，把统计字段写入飞书《达人基础信息表》。
统计字段（粉丝数 / 获赞数 / 作品数 / 账号简介 / IP 属地 / 所在地区 / 关注数 / 账号 ID / 抖音 UID / 头像 / 最近发稿时间 / 账号状态等）由「profile 采集」工序抓取后回写飞书。
字段归属与必填规则的唯一权威定义在 `scripts/creator_table_fields.py`（PROFILE 段）。

## 2. 当前流水线顺序（run_daily.bat）

1. 对账自愈：`check_and_onboard_new_creators.py --reconcile --apply`
2. 接入新达人：`check_and_onboard_new_creators.py --apply --no-collect`
3. **主流水线**（`scripts/run_creator_pipeline.py`）：**作品采集（MediaCrawler）** + 转写 + 备份
4. **profile 采集 + 回写**：`check_and_onboard_new_creators.py --collect-profiles --sync-profiles`

即：profile 采集目前在「作品采集之后串行」执行，不是并发。

## 3. 当前 profile 采集行为（已核实代码）

- **12 小时 TTL 复用**（check 脚本 `run_collect_profiles`，约 line 812–834）：
  若 `runtime/profile-<key>-update.json` 在 `profile_ttl_hours`（默认 12h，pipeline.json 未设则走默认）内，直接 `continue` **跳过采集、不打开主页**。
- **单次内重试**（collect 脚本 `collect`，约 line 543–572）：最多重试 4 次（每次等 4s），保留字段最全的一次；粉丝+获赞抓到即提前结束。
- **回写规则**（`sync_profile_to_feishu`，check 脚本约 line 565）：只对**非空**字段做 upsert，**绝不用空值覆盖飞书已有值**；一旦本地抓到之前缺失的值，就覆盖写进飞书（补写链路是通的）。

## 4. 用户质疑与期望

用户原话要点：
- 「不需要这个 12 小时的限制，因为每次我们完整走这个流水线的时候，都需要打开达人主页。判断达人信息全的情况下，就不需要走达人主页了？如果我们会打开达人的主页，那相关的这些数据我们都会有，这个 12 小时的限制是没有意义的。」
- 「每次打开都要刷，它应该跟作品采集是同时并发执行的两个流程，因为打开主页获取作品的时候，达人的这些基础信息都会有。如果说有获取到 profile 的字段，就直接覆盖更新上去。」

提炼为两条期望：
1. **每次走流水线都刷新 profile**（去掉/弱化 12h TTL），避免「第一次没抓到、之后 12h 内一直空」。
2. **profile 采集应与作品采集并发执行**；抓到的 profile 字段直接覆盖更新飞书。

## 5. 关键约束（已核实，影响并发可行性）

- **作品采集与 profile 采集对同一达人使用同一登录态目录** `cdp_<key>_dy_user_data_dir`：
  - profile 采集：`check_and_onboard_new_creators.py` 的 `_resolve_user_data_dir`（line 775–787）解析的就是 `cdp_<key>_dy_user_data_dir`。
  - 作品采集：`run_creator_pipeline.py` 的 `collect_command`（line 541 / 577）使用 `cdp_<profile_key>_dy_user_data_dir`。
- **Chrome 不允许两个浏览器实例同时打开同一 user_data_dir**，否则报 `user data directory is already in use`，两条都会失败。
- 抖音**公开主页**的统计字段（粉丝/获赞等）公开可见；`collect_douyin_creator_profile.py` 可复用任意登录态目录或全新目录进入主页（不强制本达人登录态）。

## 6. 具体问题（请 Codex 给建议）

### Q1. 去 TTL 的实现与副作用
直接删除 line 826–834 的 TTL 判断（每次都重抓）是否稳妥？对抖音风控 / 请求速率 / 登录态过期有无实质风险？
是否有比「无脑每次都抓」更优的**「按完整性复用」**策略——例如：仅当 PROFILE 必填字段（见 `creator_table_fields.py`）已齐全才跳过，有缺口则强制重抓？

### Q2. 真并发方案（登录态冲突是硬障碍）
如何在「不冲突」前提下让 profile 与作品采集并发？请评估以下方案的可行性/稳健性：
- **方案 A（调度层并发 + 换目录）**：profile 采集改用**独立登录态目录**（复用其他达人的有效 `cdp_*` 目录，或全新目录），在 `run_daily.bat` 用 `start` / PowerShell `Start-Process` 把「主流水线」与「profile 采集」并行启动，再 `Wait-Process` 汇合。
  - 需确认：用「其他达人登录态 / 全新目录」抓**公开主页**能否稳定拿到统计字段（之前实践过复用任意目录进主页，但未经并发压测）。
- **方案 B（流水线内并发）**：在 `run_creator_pipeline.py` 的 `collect_creator_phase` 内，把「作品采集」与「profile 采集」拆成同一达人的两个并行子任务（ThreadPoolExecutor），profile 用不同的 `user_data_dir`。
- **方案 C（顺序但去 TTL）**：保持现有顺序，仅去掉 TTL 让每次都刷——最简单稳妥，但不是真并发。

你推荐哪种？若选 A，换目录后抓公开主页的稳定性如何保障？

### Q3. 永久缺口防死循环
若采集脚本本身解析不到某字段（例如某达人简介 / IP 属地 DOM 未暴露），每次重抓仍为空。是否应加「连续 N 次失败则标记『异常』并暂停重试」的机制，避免无限空转浪费登录态？

### Q4. 架构级优化（是否该消除两道工序拆分）
用户设想「打开主页获取作品时，基础信息都会有」。现实是作品采集走 MediaCrawler 的抖音 API（aweme 作品列表），**不加载主页 DOM**，故不会顺带拿到统计字段。
能否、是否值得让作品采集阶段**顺带抓统计字段**（真正同一次打开主页共享数据）？还是维持「作品采集」与「profile 采集」两道工序、仅做并发与去 TTL 即可？

## 7. 相关文件速查

| 文件 | 关键内容 |
|------|----------|
| `scripts/check_and_onboard_new_creators.py` | `run_collect_profiles`（TTL line 812–834）、`_resolve_user_data_dir`（775–787）、`sync_profile_to_feishu`（只写非空，约 565） |
| `scripts/collect_douyin_creator_profile.py` | `collect`（单次重试 4 次保留最全，543–572；response 监听抓真实抖音 UID） |
| `scripts/creator_table_fields.py` | `PROFILE_FIELDS` 字段定义；IP属地/所在地区为可选字段，抓到才更新，缺失时保留历史值或忽略 |
| `scripts/run_creator_pipeline.py` | `collect_command`（作品采集登录态目录 541/577）、`collect_creator_phase`（1745+） |
| `run_daily.bat` | 调度顺序（reconcile→onboard→主流水线→profile 采集） |
| `local/pipeline.json` | `collection.profile_ttl_hours`（当前未设，走默认 12）、`collection.profile_max_workers`（默认 3） |

## 8. 当前已知事实小结

- 补写链路已通：只要某次本地抓到缺失值，sync 会覆盖写进飞书（只写非空）。
- 12h TTL 是「第一次没抓到 → 缺口滞留」的元凶之一（另一元凶是采集脚本对某些字段 DOM 解析不到，属脚本能力问题，非 sync 问题）。
- 真并发的瓶颈是「同一登录态目录不能双开 Chrome」，必须由换目录或工序内拆分来解决。
