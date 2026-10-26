// dsh-notifier src/admin/ui/theme.mjs
// 管理台样式（「宿主对齐·蓝白」视觉语言）：对齐 DeepSeek Harness Web UI 设计令牌——
// 白面板 + #f5f6f7 底 + 0.5px 发丝边框 + DeepSeek Blue(#4176e6) 主色 + 柔和微投影。
// 令牌来源：deepseek-harness packages/client/ui-theme/src/styles/{design-platform,base}.css。
// 零外部资源（无 CDN/字体包/外链图片），系统字体栈；动效只服务状态反馈，尊重 prefers-reduced-motion。
// 注意：本文件是模板字面量——内容里不得出现反引号与 ${ 序列。
export const ADMIN_UI_CSS = `:root {
  --bg: #f5f6f7; --bg-soft: #f9fafb; --panel: #ffffff; --panel2: #f5f6f7; --panel3: #eef1f5;
  --border: rgba(0, 0, 0, .06); --border-strong: rgba(0, 0, 0, .12); --border-input: rgba(0, 0, 0, .16);
  --text: #0f0f0f; --muted: #61666b; --faint: #81858c; --dim: #adb2b8;
  --accent: #4176e6; --accent-hover: #3563d0; --accent-soft: #e4edfd; --accent-softer: #edf3fe; --accent-ink: #ffffff;
  --ok: #22c55e; --ok-ink: #15803d; --ok-soft: #e6faed;
  --warn: #f59e0b; --warn-ink: #b45309; --warn-soft: #fef5e7;
  --err: #ec1313; --err-soft: rgba(236, 19, 19, .08);
  --info: #4176e6;
  --radius: 12px; --radius-s: 8px;
  --shadow: 0 0 0 .5px rgba(0, 0, 0, .04), 0 3px 8px rgba(0, 0, 0, .03), 0 0 16px rgba(0, 0, 0, .02);
  --shadow-pop: 0 0 1px rgba(0, 0, 0, .12), 0 12px 32px rgba(0, 0, 0, .10);
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Helvetica, Arial, sans-serif; }
h1, h2, h3 { font-weight: 600; }
h3 { font-size: 14px; margin: 22px 0 10px; letter-spacing: .01em; }
h3 .muted { font-weight: 400; margin-left: 8px; font-size: 12px; }
p { margin: 8px 0; }
a { color: var(--info); }
code { font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace; font-size: .92em;
  background: var(--panel2); padding: 1px 6px; border-radius: 6px; border: 1px solid var(--border); }
.skip { position: absolute; left: -9999px; top: 0; background: var(--accent); color: var(--accent-ink);
  padding: 8px 14px; border-radius: 0 0 10px 0; z-index: 60; font-weight: 600; }
.skip:focus { left: 0; }

/* ---------- 顶部：品牌 beacon + 入口 + 会话控制 ---------- */
header { display: flex; align-items: center; gap: 12px; padding: 12px 20px;
  background: rgba(255, 255, 255, .86); border-bottom: 1px solid var(--border);
  backdrop-filter: blur(8px); position: sticky; top: 0; z-index: 30; }
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brand h1 { font-size: 15px; margin: 0; white-space: nowrap; }
.brand h1 small { color: var(--faint); font-weight: 400; margin-left: 6px; font-size: 11px; }
.beacon { color: var(--accent); display: inline-flex; align-items: center; flex: 0 0 auto; }
.beacon svg { display: block; }
#loadState { color: var(--accent); font-size: 12px; min-width: 4em; }
#entryHint { color: var(--muted); font-size: 12px; min-width: 0; overflow-wrap: anywhere; flex: 1 1 220px; }
#entryUrl { color: var(--text); }
#tokenState { border-style: dashed; }
header button { flex: 0 0 auto; }

/* ---------- 主导航：分段控件 ---------- */
nav { display: flex; gap: 4px; padding: 10px 20px 0; flex-wrap: wrap; align-items: center; }
.tabbtn { border-radius: 999px; border-color: transparent; background: transparent; color: var(--muted); padding: 6px 14px; }
.tabbtn:hover { color: var(--text); background: var(--panel3); border-color: transparent; }
.tabbtn.active { background: var(--accent-soft); border-color: transparent; color: var(--accent); font-weight: 600; }
.tabsec { display: none; }
.tabsec.active { display: block; animation: rise .18s ease-out; }
#modeToggle { margin-left: auto; }

main { padding: 16px 20px 56px; max-width: 1080px; margin: 0 auto; }

/* ---------- 控件 ---------- */
button { background: var(--panel); color: var(--text); border: 1px solid var(--border-strong);
  border-radius: 999px; padding: 6px 16px; cursor: pointer; font: inherit; font-size: 13px;
  transition: border-color .16s, background .16s, color .16s, transform .1s; }
button:hover { border-color: var(--accent); color: var(--accent); }
button:active { transform: translateY(1px); }
button:disabled { opacity: .45; cursor: default; transform: none; }
button.danger { color: var(--err); }
button.danger:hover { border-color: var(--err); background: var(--err-soft); }
.btn-primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); font-weight: 600; }
.btn-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); color: var(--accent-ink); }
.muted-btn { background: transparent; border: 1px solid var(--border-strong); color: var(--muted); font-size: 12px; padding: 3px 12px; }
.muted-btn:hover { color: var(--accent); }
input, select { background: var(--panel); color: var(--text); border: 1px solid var(--border-input);
  border-radius: var(--radius-s); padding: 6px 10px; font: inherit; font-size: 13px; }
input::placeholder { color: var(--dim); }
input:focus, select:focus { border-color: var(--accent); }
input:focus, select:focus, button:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
input:focus:not(:focus-visible), select:focus:not(:focus-visible), button:focus:not(:focus-visible) { outline: none; }
input:focus-visible, select:focus-visible, button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
input.wide { width: 100%; }
input.mono, .mono { font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace; font-size: 13px; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.center { text-align: center; }
.wrap { max-width: 560px; }
.empty { color: var(--faint); text-align: center; padding: 16px; }
.row { display: flex; gap: 8px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
label.ck { display: inline-flex; align-items: center; gap: 4px; margin: 0 10px 4px 0; white-space: nowrap; font-size: 13px; }
label.fld { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
label.fld > span { width: 190px; color: var(--muted); flex: 0 0 auto; }
label.fld input { flex: 1; min-width: 0; }

/* ---------- 消息条 ---------- */
.msg, .inline { display: none; }
.msg.show, .inline.show { display: block; }
.msg { border: 1px solid var(--border); border-left-width: 3px; border-radius: var(--radius-s);
  padding: 9px 14px; margin-bottom: 14px; background: var(--panel); }
.inline { margin-top: 10px; font-size: 13px; }
.msg.ok, .inline.ok { color: var(--ok-ink); border-left-color: var(--ok); }
.msg.err, .inline.err { color: var(--err); border-left-color: var(--err); }
.msg.warn, .inline.warn { color: var(--warn-ink); border-left-color: var(--warn); }

/* ---------- 解锁门（无 token / 401 的站内解锁页；绝不弹 window.prompt） ---------- */
.gate { position: fixed; inset: 0; z-index: 50; display: flex; align-items: center; justify-content: center;
  padding: 24px; background: rgba(245, 246, 247, .82); backdrop-filter: blur(10px); }
.gate[hidden] { display: none; }
.gate-card { width: 100%; max-width: 420px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 24px; padding: 34px 30px 26px; box-shadow: var(--shadow-pop); text-align: center;
  animation: rise .22s ease-out; }
.gate-card .beacon { color: var(--accent); margin-bottom: 6px; position: relative; }
.gate-card .beacon::after { content: ""; position: absolute; inset: -10px; border-radius: 50%;
  border: 1px solid var(--accent); opacity: .35; animation: pulse 2.4s ease-out infinite; }
.gate-card h1 { font-size: 19px; margin: 10px 0 2px; }
.gate-sub { color: var(--muted); font-size: 12.5px; margin: 0 0 16px; letter-spacing: .04em; }
.gate-note { color: var(--muted); font-size: 13px; margin: 0 0 14px; text-align: left; }
.gate-label { display: block; text-align: left; color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.gate-field { display: flex; gap: 8px; }
.gate-field input { flex: 1; min-width: 0; font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace; }
.gate-err { color: var(--err); background: var(--err-soft); border: 1px solid rgba(236, 19, 19, .3);
  border-radius: var(--radius-s); padding: 8px 12px; font-size: 12.5px; text-align: left; margin: 12px 0 0; }
.gate-actions { margin-top: 16px; display: flex; gap: 10px; justify-content: center; }
.gate-actions .btn-primary { flex: 1; padding: 9px 14px; }
.gate-hint { margin-top: 18px; text-align: left; line-height: 1.7; }

/* ---------- 首页英雄区：链路状态一眼可见 ---------- */
.hero { display: flex; gap: 18px; align-items: center; background: var(--panel); border: 1px solid var(--border);
  border-radius: 16px; padding: 18px 22px; margin-bottom: 14px; box-shadow: var(--shadow); }
.hero .beacon { color: var(--accent); }
.hero .beacon.dim { color: var(--dim); }
.hero-main { flex: 1; min-width: 0; }
.hero-state { font-size: 19px; font-weight: 700; letter-spacing: .01em; }
.hero-state.ok { color: var(--ok-ink); } .hero-state.warn { color: var(--warn-ink); } .hero-state.none { color: var(--muted); }
.hero-sub { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
.hero-rail { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 10px; }
.hero-rail .state-node { display: inline-flex; gap: 5px; align-items: center; padding: 3px 10px;
  border: 1px solid var(--border-strong); border-radius: 999px; color: var(--faint); font-size: 12px; background: var(--panel); }
.hero-rail .state-node.current { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
.hero-rail .state-node.done { color: var(--ok-ink); border-color: rgba(34, 197, 94, .4); }
.hero-rail .state-arrow { color: var(--dim); font-size: 11px; }

/* 下一步行动卡 */
.nextcard { display: flex; gap: 14px; align-items: center; border-radius: 14px; padding: 13px 18px;
  margin-bottom: 16px; border: 1px solid var(--border); background: var(--panel); }
.nextcard.todo { border-color: rgba(65, 118, 230, .35); background: var(--accent-softer); }
.nextcard.ok { border-color: rgba(34, 197, 94, .3); }
.nextcard .nc-ico { flex: 0 0 auto; color: var(--accent); }
.nextcard.ok .nc-ico { color: var(--ok); }
.nextcard b { display: block; font-size: 14px; }
.nextcard p { margin: 2px 0 0; color: var(--muted); font-size: 12.5px; }
.nextcard .row { margin: 0 0 0 auto; flex: 0 0 auto; }

/* ---------- 统计与矩阵 ---------- */
.stats { display: flex; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 12px 20px; min-width: 128px; flex: 1 1 128px; }
.stat b { display: block; font-size: 24px; font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace; font-weight: 650; }
.stat span { color: var(--muted); font-size: 12px; }
.group { margin: 6px 0 12px; }
.gtitle { font-size: 12.5px; margin-bottom: 6px; color: var(--muted); }
.gtitle.ok { color: var(--ok-ink); } .gtitle.warn { color: var(--warn-ink); } .gtitle.none { color: var(--faint); }
.chip { display: inline-block; padding: 2px 10px; margin: 2px; border-radius: 999px; font-size: 12px;
  border: 1px solid var(--border-strong); font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace; }
.chip.ok { color: var(--ok-ink); border-color: rgba(34, 197, 94, .4); background: var(--ok-soft); }
.chip.warn { color: var(--warn-ink); border-color: rgba(245, 158, 11, .4); background: var(--warn-soft); }
.chip.none { color: var(--muted); }
.chip.err { color: var(--err); border-color: rgba(236, 19, 19, .3); background: var(--err-soft); }

/* ---------- 首次配置向导 ---------- */
.setup { border: 1px solid rgba(65, 118, 230, .3); border-radius: 18px; background: var(--panel);
  padding: 24px 26px 18px; margin-bottom: 18px; box-shadow: var(--shadow); }
.setup-head h2 { font-size: 20px; margin: 0 0 6px; }
.setup-head p { color: var(--muted); margin: 0 0 4px; max-width: 640px; }
.rail { list-style: none; display: flex; gap: 0; padding: 0; margin: 18px 0 20px; flex-wrap: wrap; }
.rail li { display: flex; align-items: center; gap: 8px; color: var(--faint); font-size: 12.5px; }
.rail li .rn { width: 24px; height: 24px; border-radius: 50%; border: 1.5px solid var(--border-strong);
  display: inline-flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 650; }
.rail li.current { color: var(--accent); }
.rail li.current .rn { border-color: var(--accent); background: var(--accent-soft); }
.rail li.done { color: var(--ok-ink); }
.rail li.done .rn { border-color: var(--ok); background: var(--ok-soft); }
.rail li + li::before { content: ""; width: 26px; height: 1px; background: var(--border-strong); margin: 0 10px; }
.rail li.done + li::before { background: rgba(34, 197, 94, .5); }
.setup-pane { animation: rise .18s ease-out; }
.setup-foot { margin-top: 16px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }

/* 渠道选择瓷砖 */
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; margin-top: 12px; }
.tile { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; text-align: left;
  background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 12px 14px; cursor: pointer; min-height: 86px; }
.tile:hover { border-color: var(--accent); background: var(--accent-softer); }
.tile .tile-glyph { width: 30px; height: 30px; border-radius: 9px; background: var(--accent-soft);
  color: var(--accent); display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 13px; margin-bottom: 4px; font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace; }
.tile b { font-size: 13.5px; }
.tile .tile-tag { color: var(--faint); font-size: 11.5px; line-height: 1.45; }
.tile .tile-rec { color: var(--accent); font-size: 11px; border: 1px solid rgba(65, 118, 230, .4);
  border-radius: 999px; padding: 0 8px; margin-top: 5px; }
.tile.more { align-items: center; justify-content: center; color: var(--muted); min-height: 86px; border-style: dashed; }

/* 向导测试态 */
.setup-test { display: flex; gap: 14px; align-items: flex-start; background: var(--bg-soft);
  border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 16px; margin-top: 10px; }
.setup-test .st-ico { flex: 0 0 auto; margin-top: 2px; }
.spinner { width: 18px; height: 18px; border-radius: 50%; border: 2px solid var(--border-strong);
  border-top-color: var(--accent); animation: spin .9s linear infinite; }
.setup-done { text-align: center; padding: 18px 0 8px; }
.setup-done .beacon { color: var(--ok); }
.setup-done h3 { font-size: 18px; margin: 12px 0 6px; color: var(--ok-ink); }
.setup-done p { color: var(--muted); max-width: 520px; margin: 6px auto; }
.setup-done .row { justify-content: center; margin-top: 16px; }

/* 验证横幅（升级老用户：已启用但本浏览器未验证送达） */
.verify { display: flex; gap: 12px; align-items: center; border: 1px solid rgba(245, 158, 11, .4);
  background: var(--warn-soft); border-radius: 14px;
  padding: 12px 18px; margin-bottom: 16px; flex-wrap: wrap; }
.verify b { color: var(--warn-ink); }
.verify .row { margin: 0 0 0 auto; }

/* ---------- 卡片与表格 ---------- */
.card { border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 10px; background: var(--panel);
  transition: border-color .12s ease, box-shadow .12s ease; }
.card:hover { border-color: var(--border-strong); box-shadow: var(--shadow); }
/* 已配置渠道卡片：左侧品牌蓝强调条，一眼区分就绪渠道 */
.card:has(.badge.ok), .card:has(.badge.warn) { border-left: 3px solid var(--accent); }
.card:has(.badge.warn) { border-left-color: var(--warn); }
.card-head { display: flex; gap: 10px; align-items: center; padding: 10px 14px; cursor: pointer;
  border-radius: var(--radius) var(--radius) 0 0; }
.card-head:hover { background: var(--bg-soft); }
/* 展开指示箭头：默认收起朝右，展开朝下 */
.card-head::after { content: ""; margin-left: 2px; flex: 0 0 auto; width: 8px; height: 8px;
  border-right: 2px solid var(--faint); border-bottom: 2px solid var(--faint);
  transform: rotate(-45deg); transition: transform .12s ease; }
.card:has(.card-body:not([hidden])) .card-head::after { transform: rotate(45deg); }
.card-head .badge { margin-left: auto; }
.card-head .glyph { width: 26px; height: 26px; border-radius: 8px; background: var(--accent-soft); color: var(--accent);
  display: inline-flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700;
  font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace; flex: 0 0 auto; }
.card-body { padding: 6px 14px 14px; border-top: 1px solid var(--border); animation: rise .15s ease-out; }

/* 更多渠道折叠组：原生 details，默认收起，减少首屏纵向噪音 */
.more-group { border: 1px dashed var(--border-strong); border-radius: var(--radius);
  margin: 4px 0 14px; background: var(--bg-soft); }
.more-group > summary { list-style: none; cursor: pointer; padding: 11px 16px; display: flex;
  align-items: center; gap: 8px; color: var(--muted); font-size: 13.5px; user-select: none; }
.more-group > summary::-webkit-details-marker { display: none; }
.more-group > summary::before { content: ""; width: 8px; height: 8px; flex: 0 0 auto;
  border-right: 2px solid var(--faint); border-bottom: 2px solid var(--faint);
  transform: rotate(-45deg); transition: transform .12s ease; }
.more-group[open] > summary::before { transform: rotate(45deg); }
.more-group > summary:hover { color: var(--text); }
.more-group .more-body { padding: 4px 12px 8px; border-top: 1px dashed var(--border); }
.badge { font-size: 11.5px; font-weight: 600; letter-spacing: .01em; padding: 2px 10px; border-radius: 999px;
  border: 1px solid var(--border-strong); white-space: nowrap; }
.badge.ok { color: var(--ok-ink); border-color: rgba(34, 197, 94, .4); background: var(--ok-soft); }
.badge.none { color: var(--faint); background: var(--panel2); }
.badge.warn { color: var(--warn-ink); border-color: rgba(245, 158, 11, .4); background: var(--warn-soft); }
.dir-tag { font-size: 11px; color: var(--faint); border: 1px solid var(--border-strong); border-radius: 4px; padding: 0 6px; }
table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); overflow: hidden; }
th, td { padding: 8px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { color: var(--muted); font-weight: 500; font-size: 12px; background: var(--bg-soft); }
tr.editor td { background: var(--bg-soft); }
.edbox { padding: 6px 2px; }
.qr .mono { background: var(--panel2); padding: 3px 8px; border-radius: 6px; word-break: break-all; }
.qopts { margin: 6px 0; padding-left: 18px; }
.qopts li { margin: 4px 0; }

/* 配对码 */
.paircode { display: none; margin: 10px 0; padding: 14px 16px; border: 1px dashed rgba(34, 197, 94, .5);
  border-radius: var(--radius); background: var(--ok-soft); }
.paircode.show { display: block; }
.paircode .code { font-family: "SF Mono", "JetBrains Mono", "Fira Code", Consolas, Menlo, monospace;
  font-size: 26px; letter-spacing: 4px; color: var(--ok-ink); word-break: break-all; }
.paircode .ttl { margin-top: 6px; font-size: 13px; color: var(--muted); }

.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; flex: 0 0 auto; }
.dot.ok { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); }
.dot.off { background: var(--dim); }
.dot.warn { background: var(--warn); box-shadow: 0 0 0 3px var(--warn-soft); }

.auditlist { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 6px 14px; max-height: 300px; overflow: auto; }
.auditrow { display: flex; gap: 10px; padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.auditrow:last-child { border-bottom: 0; }
.auditrow .at { color: var(--faint); white-space: nowrap; }
.auditrow b { white-space: nowrap; }

.chan-group-title { display: flex; align-items: baseline; gap: 10px; }
.chan-group-title .dir-tag { position: relative; top: -1px; }

/* ---------- 动效（只服务状态反馈） ---------- */
@keyframes pulse { 0% { transform: scale(.72); opacity: .5; } 70% { transform: scale(1.28); opacity: 0; } 100% { opacity: 0; } }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes rise { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
}

/* ---------- 窄屏（≤768px）：单列 / 导航横滚 / 宽表横滚 / 触控目标 ≥44px ---------- */
@media (max-width: 768px) {
  header { flex-wrap: wrap; padding: 10px 12px; gap: 8px; }
  #entryHint { flex-basis: 100%; order: 2; }
  main { padding: 12px 10px 44px; }
  nav { flex-wrap: nowrap; overflow-x: auto; padding: 8px 10px 0; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
  nav::-webkit-scrollbar { display: none; }
  .tabbtn { flex: 0 0 auto; }
  #modeToggle { margin-left: 0; }
  label.fld { flex-direction: column; align-items: stretch; gap: 4px; }
  label.fld > span { width: auto; }
  table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  button { min-height: 44px; }
  input, select { min-height: 44px; font-size: 16px; } /* 16px：iOS 聚焦不触发页面自动缩放 */
  .stat { min-width: 104px; padding: 10px 14px; flex: 1 1 40%; }
  .hero { flex-direction: column; align-items: flex-start; gap: 10px; }
  .nextcard { flex-wrap: wrap; }
  .nextcard .row { margin-left: 0; }
  .setup { padding: 18px 14px 14px; }
  .tiles { grid-template-columns: repeat(auto-fill, minmax(128px, 1fr)); }
  .rail li + li::before { width: 12px; margin: 0 6px; }
  .gate-card { padding: 26px 18px 20px; }
}
`
