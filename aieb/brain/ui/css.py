"""AI 外脑 Demo 的设计系统（CSS）。

视觉规范：深蓝主色 #1E3A5F、品牌蓝 #2563EB、辅助青绿 #0D9488，
浅灰底 #F8FAFC、白卡片、统一 8px 间距体系与 10/14px 圆角，
风险等级使用语义色（高=红、中=橙、低=灰蓝、完成=绿）。
"""

STYLE = """
:root{
  --navy:#1E3A5F; --brand:#2563EB; --brand-dark:#1D4ED8; --teal:#0D9488;
  --bg:#F8FAFC; --card:#FFFFFF; --text:#0F172A; --muted:#64748B; --line:#E2E8F0;
  --red:#DC2626; --red-bg:#FEF2F2; --orange:#EA580C; --orange-bg:#FFF7ED;
  --green:#059669; --green-bg:#ECFDF5; --blue-bg:#EFF6FF;
  --radius:14px; --radius-sm:10px;
  --shadow:0 1px 2px rgba(15,23,42,.04), 0 8px 24px rgba(15,23,42,.06);
  --shadow-sm:0 1px 2px rgba(15,23,42,.06);
  --sidebar-w:236px;
  font-size:16px;
}
*{box-sizing:border-box;}
html,body{height:100%;}
body{
  margin:0; background:var(--bg); color:var(--text);
  font-family:"PingFang SC","Microsoft YaHei","Segoe UI",system-ui,-apple-system,sans-serif;
  font-size:14px; line-height:1.6; -webkit-font-smoothing:antialiased;
}
button,input,textarea,select{font-family:inherit;font-size:14px;color:inherit;}
a{color:var(--brand);text-decoration:none;}
h1,h2,h3,h4{margin:0;font-weight:650;letter-spacing:-.01em;}
::-webkit-scrollbar{width:10px;height:10px;}
::-webkit-scrollbar-thumb{background:#CBD5E1;border-radius:8px;border:2px solid transparent;background-clip:content-box;}
::-webkit-scrollbar-track{background:transparent;}

/* ── 布局 ───────────────────────────────────────────── */
.app{display:flex;height:100vh;overflow:hidden;}
.sidebar{
  width:var(--sidebar-w);flex:0 0 var(--sidebar-w);background:var(--navy);
  color:#E2E8F0;display:flex;flex-direction:column;padding:18px 12px;
}
.brand{display:flex;align-items:center;gap:10px;padding:0 8px 18px;}
.brand-mark{
  width:38px;height:38px;border-radius:11px;background:linear-gradient(145deg,#2563EB,#0D9488);
  display:grid;place-items:center;flex:0 0 38px;box-shadow:0 4px 14px rgba(37,99,235,.35);
}
.brand-mark svg{width:22px;height:22px;}
.brand-name{font-size:15px;font-weight:700;color:#fff;line-height:1.25;}
.brand-sub{font-size:11px;color:#94A3B8;letter-spacing:.04em;text-transform:uppercase;}
.nav{display:flex;flex-direction:column;gap:2px;flex:1;overflow-y:auto;}
.nav-item{
  display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:var(--radius-sm);
  color:#CBD5E1;background:none;border:0;cursor:pointer;text-align:left;width:100%;
  transition:background .15s,color .15s;
}
.nav-item:hover{background:rgba(255,255,255,.08);color:#fff;}
.nav-item.active{background:#2563EB;color:#fff;box-shadow:0 6px 16px rgba(37,99,235,.35);}
.nav-item svg{width:17px;height:17px;flex:0 0 17px;opacity:.9;}
.nav-badge{margin-left:auto;font-size:11px;background:rgba(255,255,255,.16);padding:1px 7px;border-radius:20px;}
.sidebar-foot{border-top:1px solid rgba(255,255,255,.1);padding-top:12px;margin-top:8px;display:flex;flex-direction:column;gap:8px;}
.side-meta{font-size:11px;color:#94A3B8;display:flex;align-items:center;gap:6px;}
.dot{width:7px;height:7px;border-radius:50%;background:#94A3B8;display:inline-block;}
.dot.on{background:#10B981;box-shadow:0 0 0 3px rgba(16,185,129,.2);}
.dot.off{background:#EF4444;}

.main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden;}
.topbar{
  display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding:16px 24px;background:var(--card);border-bottom:1px solid var(--line);
}
.topbar h1{font-size:18px;}
.topbar .sub{margin:2px 0 0;font-size:12px;color:var(--muted);}
.top-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.content{flex:1;overflow-y:auto;padding:24px;}
.content.flush{padding:0;overflow:hidden;display:flex;}

/* ── 通用组件 ───────────────────────────────────────── */
.btn{
  display:inline-flex;align-items:center;gap:6px;justify-content:center;
  padding:8px 14px;border-radius:var(--radius-sm);border:1px solid var(--line);
  background:var(--card);color:var(--text);cursor:pointer;font-weight:500;
  min-height:36px;white-space:nowrap;transition:background .15s,border-color .15s,box-shadow .15s;
}
.btn:hover{background:#F1F5F9;border-color:#CBD5E1;}
.btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px;}
.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff;}
.btn.primary:hover{background:var(--brand-dark);border-color:var(--brand-dark);}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted);}
.btn.ghost:hover{background:#F1F5F9;color:var(--text);}
.btn.danger{color:var(--red);border-color:#FECACA;background:var(--red-bg);}
.btn.danger:hover{background:#FEE2E2;}
.btn.sm{padding:5px 10px;min-height:30px;font-size:13px;}
.btn[disabled]{opacity:.55;cursor:not-allowed;}
.icon-btn{width:32px;height:32px;display:grid;place-items:center;border-radius:8px;border:0;background:transparent;color:var(--muted);cursor:pointer;}
.icon-btn:hover{background:#F1F5F9;color:var(--text);}

.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow-sm);}
.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 18px;border-bottom:1px solid var(--line);}
.card-head h3{font-size:14px;}
.card-body{padding:16px 18px;}
.muted{color:var(--muted);}
.small{font-size:12px;}
.mono{font-family:"Cascadia Code",Consolas,monospace;font-size:12px;}
.row{display:flex;align-items:center;gap:8px;}
.row.wrap{flex-wrap:wrap;}
.spread{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.stack{display:flex;flex-direction:column;gap:12px;}
.grow{flex:1;min-width:0;}
.truncate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:20px;font-size:12px;background:#F1F5F9;color:var(--muted);border:1px solid var(--line);flex:0 0 auto;white-space:nowrap;line-height:1.7;}
.chip.high{background:var(--red-bg);color:var(--red);border-color:#FECACA;}
.chip.medium{background:var(--orange-bg);color:var(--orange);border-color:#FED7AA;}
.chip.low{background:var(--blue-bg);color:#1D4ED8;border-color:#BFDBFE;}
.chip.done{background:var(--green-bg);color:var(--green);border-color:#A7F3D0;}
.chip.brand{background:var(--blue-bg);color:var(--brand);border-color:#BFDBFE;}
.chip.warn{background:#FFFBEB;color:#B45309;border-color:#FDE68A;}
.chip.err{background:var(--red-bg);color:var(--red);border-color:#FECACA;}

.field{display:flex;flex-direction:column;gap:6px;}
.field label{font-size:12px;color:var(--muted);font-weight:500;}
.input,.textarea,.select{
  width:100%;padding:9px 12px;border:1px solid var(--line);border-radius:var(--radius-sm);
  background:#fff;transition:border-color .15s,box-shadow .15s;
}
.input:focus,.textarea:focus,.select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.12);}
.textarea{resize:vertical;min-height:90px;line-height:1.65;}

.empty{
  text-align:center;padding:36px 20px;color:var(--muted);
  border:1px dashed var(--line);border-radius:var(--radius);background:#FCFDFE;
}
.empty .emoji{font-size:24px;display:block;margin-bottom:8px;opacity:.65;}
.empty h4{font-size:14px;color:var(--text);margin-bottom:4px;}

/* ── 工作台 ─────────────────────────────────────────── */
.hero{
  background:linear-gradient(120deg,#1E3A5F 0%,#1E3A5F 55%,#0D9488 160%);
  color:#fff;border-radius:18px;padding:28px 30px;margin-bottom:20px;position:relative;overflow:hidden;
}
.hero:after{content:"";position:absolute;right:-60px;top:-80px;width:260px;height:260px;border-radius:50%;background:rgba(255,255,255,.06);}
.hero h2{font-size:24px;margin-bottom:6px;}
.hero p{margin:0;color:#CBD5E1;font-size:14px;max-width:620px;}
.quick-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px;}
.quick{
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px;
  display:flex;gap:12px;align-items:center;cursor:pointer;text-align:left;
  transition:transform .15s,box-shadow .15s,border-color .15s;
}
.quick:hover{transform:translateY(-2px);box-shadow:var(--shadow);border-color:#CBD5E1;}
.quick .q-icon{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;flex:0 0 36px;align-self:center;background:var(--blue-bg);color:var(--brand);}
.quick .q-icon svg{display:block;}
.quick .q-icon.teal{background:#F0FDFA;color:var(--teal);}
.quick .q-icon.navy{background:#EEF2F7;color:var(--navy);}
.quick .q-icon.amber{background:var(--orange-bg);color:var(--orange);}
/* 图标与右侧文字块整体垂直居中：文字块用固定行高，避免行距把标题压低 */
.quick .q-text{display:block;min-width:0;line-height:1.35;color:inherit;}
.quick .q-text strong{display:block;font-size:14px;line-height:1.35;margin-bottom:2px;color:var(--navy);}
.quick .q-text .q-desc{display:block;font-size:12px;line-height:1.45;color:var(--muted);}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px;}
.stat{padding:16px 18px;}
.stat .label{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px;}
.stat .value{font-size:26px;font-weight:700;margin-top:6px;letter-spacing:-.02em;}
.stat .value.warn{color:var(--red);}
.cols{display:grid;grid-template-columns:1.15fr 1fr;gap:16px;}
.list-item{display:flex;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);align-items:flex-start;}
.list-item:last-child{border-bottom:0;padding-bottom:0;}
.list-item:first-child{padding-top:0;}
/* 右侧操作按钮默认会被 flex 拉伸成整行高，统一垂直居中 */
.list-item > .btn,.list-item > .chip{align-self:center;}
.li-main{min-width:0;flex:1;}
.li-title{font-weight:550;font-size:13.5px;margin-bottom:2px;}
.li-sub{font-size:12px;color:var(--muted);}
/* 记忆条目：标签不压缩、标题可行内换行，避免标签被挤成两行汉字 */
.mem-sec{
  display:flex;align-items:baseline;justify-content:space-between;gap:10px;
  padding:14px 0 6px;font-size:12.5px;font-weight:600;color:var(--navy);
  border-bottom:1px dashed var(--line);margin-bottom:4px;
}
.mem-sec:first-child{padding-top:0;}
.mem-row{display:flex;align-items:flex-start;gap:8px;}
/* 记忆条目的勾选框：与标题基线对齐 */
.mem-chk{display:flex;align-items:center;padding-top:2px;flex:0 0 15px;}
.mem-chk input{width:15px;height:15px;cursor:pointer;accent-color:var(--brand);}
.mem-title{
  margin:0;min-width:0;line-height:1.55;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
}
.feed-dot{width:8px;height:8px;border-radius:50%;background:var(--brand);margin-top:7px;flex:0 0 8px;}
/* 活动记录的单条删除：低调常显，悬停变红，避免用户找不到入口 */
.act-del{width:24px;height:24px;font-size:12px;flex:0 0 24px;align-self:center;color:#94A3B8;}
.act-del:hover{background:var(--orange-bg);color:var(--red);}

/* ── 对话 ──────────────────────────────────────────── */
.chat{display:flex;width:100%;min-height:0;}
.chat-sessions{
  width:250px;flex:0 0 250px;border-right:1px solid var(--line);background:var(--card);
  display:flex;flex-direction:column;min-height:0;
}
.chat-sessions .cs-head{padding:14px;border-bottom:1px solid var(--line);display:flex;gap:8px;}
.cs-list{overflow-y:auto;padding:8px;flex:1;}
.cs-item{padding:9px 10px;border-radius:10px;cursor:pointer;border:1px solid transparent;display:flex;align-items:center;gap:6px;}
.cs-item:hover{background:#F8FAFC;}
.cs-item.active{background:var(--blue-bg);border-color:#BFDBFE;}
.cs-item .cs-main{min-width:0;flex:1;}
.cs-item .t{font-size:13px;font-weight:550;}
.cs-item .m{font-size:11px;color:var(--muted);}
/* 会话操作入口：平时隐藏，悬停/选中/编辑时出现，避免侧栏一直很吵 */
.cs-item .cs-ops{display:none;gap:2px;flex:0 0 auto;}
.cs-item:hover .cs-ops,.cs-item.active .cs-ops,.cs-item.editing .cs-ops{display:flex;}
.cs-item .cs-ops .icon-btn{width:22px;height:22px;font-size:12px;color:#64748B;}
.cs-item .cs-ops .icon-btn:hover{background:#E2E8F0;color:var(--brand);}
.cs-input{
  width:100%;font:inherit;font-size:13px;padding:3px 6px;border-radius:6px;
  border:1px solid var(--brand);outline:none;background:#fff;color:var(--navy);
}
/* .btn/.icon-btn 自带 display，会盖掉浏览器对 [hidden] 的默认样式，
   导致“发送”和“停止”同时显示——这里统一强制隐藏 */
[hidden]{display:none !important;}
.chat-main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg);}
.chat-scroll{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:18px;}
.msg{display:flex;gap:12px;max-width:860px;width:100%;}
.msg.user{align-self:flex-end;flex-direction:row-reverse;}
.avatar{width:30px;height:30px;border-radius:9px;flex:0 0 30px;display:grid;place-items:center;font-size:12px;font-weight:700;}
.avatar.ai{background:linear-gradient(145deg,#2563EB,#0D9488);color:#fff;}
.avatar.me{background:#E2E8F0;color:var(--navy);}
.bubble{padding:13px 16px;border-radius:14px;background:var(--card);border:1px solid var(--line);box-shadow:var(--shadow-sm);min-width:0;}
.msg.user .bubble{background:var(--navy);color:#F8FAFC;border-color:var(--navy);}
.bubble p:first-child{margin-top:0;}
.bubble p:last-child{margin-bottom:0;}
.bubble table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px;}
.bubble th,.bubble td{border:1px solid var(--line);padding:7px 9px;text-align:left;}
.bubble th{background:#F8FAFC;}
.bubble code{background:#F1F5F9;padding:1px 5px;border-radius:5px;font-size:12.5px;}
.bubble pre{background:#0F172A;color:#E2E8F0;padding:12px 14px;border-radius:10px;overflow-x:auto;}
.bubble pre code{background:none;color:inherit;padding:0;}
.bubble blockquote{margin:8px 0;padding-left:12px;border-left:3px solid var(--line);color:var(--muted);}
.msg.user .bubble a{color:#93C5FD;}
.tool-line{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);padding:6px 2px;}
.spinner{width:13px;height:13px;border:2px solid #CBD5E1;border-top-color:var(--brand);border-radius:50%;animation:spin .8s linear infinite;flex:0 0 13px;}
@keyframes spin{to{transform:rotate(360deg);}}
.attach-card{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-top:10px;background:#FCFDFE;}
/* 聊天里的邮件草稿卡片 */
.mail-card{border:1px solid #BFDBFE;border-radius:12px;background:#F8FAFF;padding:12px 14px;margin-top:10px;max-width:560px;}
.mail-card .mail-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:8px;font-size:13.5px;}
.mail-card .mail-row{display:flex;gap:8px;font-size:12.5px;margin-bottom:4px;}
.mail-card .mail-row .muted{flex:0 0 44px;}
.mail-card .mail-body{
  font-size:12.5px;line-height:1.6;color:#334155;background:#fff;border:1px solid var(--line);
  border-radius:8px;padding:8px 10px;margin:8px 0;white-space:pre-wrap;
}
.mail-card .mail-todos ul{margin:4px 0 0 16px;font-size:12px;color:#475569;}
.mail-card .mail-foot{display:flex;align-items:center;gap:10px;margin-top:10px;}
.attach-card .fname{font-size:13px;font-weight:550;}
.chat-composer{border-top:1px solid var(--line);background:var(--card);padding:12px 24px 16px;}
.quick-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;}
.quick-chip{
  display:inline-flex;align-items:center;gap:6px;font-size:12.5px;padding:5px 11px;border-radius:20px;
  border:1px solid var(--line);background:#fff;color:var(--muted);cursor:pointer;
}
.quick-chip:hover{border-color:#BFDBFE;color:var(--brand);background:var(--blue-bg);}
.composer-box{display:flex;gap:10px;align-items:flex-end;border:1px solid var(--line);border-radius:14px;padding:10px;background:#fff;box-shadow:var(--shadow-sm);}
.composer-box:focus-within{border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.1);}
.composer-box textarea{flex:1;border:0;outline:none;resize:none;max-height:160px;line-height:1.6;padding:4px 2px;}
.composer-actions{display:flex;gap:6px;align-items:center;}
.attach-tag{display:inline-flex;align-items:center;gap:6px;background:var(--blue-bg);color:var(--brand);border:1px solid #BFDBFE;border-radius:8px;padding:3px 8px;font-size:12px;margin:0 6px 6px 0;}
/* 附件用途：PDF 只显示固定标签，TXT/MD 可切换“会议记录 / 知识库” */
.attach-tag .att-kind{font-size:11px;padding:0 6px;border-radius:6px;background:#fff;border:1px solid #BFDBFE;color:var(--brand);}
.attach-tag .att-sel{
  font:inherit;font-size:11px;padding:1px 4px;border-radius:6px;border:1px solid #BFDBFE;
  background:#fff;color:var(--brand);cursor:pointer;
}
.hint{font-size:11px;color:var(--muted);margin-top:6px;}

/* ── 知识库 / 会议 双栏 ─────────────────────────────── */
.split{display:grid;grid-template-columns:320px 1fr;gap:16px;min-height:0;}
.panel-list{display:flex;flex-direction:column;min-height:0;max-height:calc(100vh - 190px);}
.panel-list .scroll{overflow-y:auto;padding:8px;}
.doc-item{padding:10px 12px;border-radius:10px;border:1px solid transparent;cursor:pointer;}
.doc-item:hover{background:#F8FAFC;}
.doc-item.active{background:var(--blue-bg);border-color:#BFDBFE;}
.doc-item .n{font-size:13px;font-weight:550;word-break:break-all;}
.doc-item .m{font-size:11.5px;color:var(--muted);margin-top:2px;}
.drop{
  border:1.5px dashed #CBD5E1;border-radius:var(--radius);padding:22px;text-align:center;color:var(--muted);
  background:#FCFDFE;cursor:pointer;transition:border-color .15s,background .15s;
}
.drop:hover,.drop.drag{border-color:var(--brand);background:var(--blue-bg);color:var(--brand);}

.meeting-overview{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px;}
.ov{background:#F8FAFC;border:1px solid var(--line);border-radius:var(--radius-sm);padding:12px;}
.ov .k{font-size:11.5px;color:var(--muted);}
.ov .v{font-size:13.5px;font-weight:600;margin-top:3px;word-break:break-word;}
.risk-card{border:1px solid var(--line);border-left:3px solid var(--muted);border-radius:10px;padding:12px 14px;background:#fff;margin-bottom:10px;}
.risk-card.high{border-left-color:var(--red);}
.risk-card.medium{border-left-color:var(--orange);}
.risk-card.low{border-left-color:#3B82F6;}
.risk-card .rh{display:flex;align-items:center;gap:8px;margin-bottom:6px;}
.risk-card .ev{font-size:12px;color:var(--muted);background:#F8FAFC;border-radius:8px;padding:8px 10px;margin-top:8px;border:1px solid var(--line);}

.table{width:100%;border-collapse:collapse;font-size:13px;}
.table th{text-align:left;font-size:11.5px;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line);background:#F8FAFC;white-space:nowrap;}
.table td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top;}
.table tr:last-child td{border-bottom:0;}
.table tr:hover td{background:#FCFDFE;}
/* 批量选择：表格里的勾选框行高对齐 */
.table input[type=checkbox]{width:15px;height:15px;cursor:pointer;accent-color:var(--brand);}
.chk-all{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);cursor:pointer;white-space:nowrap;}
.chk-all input{width:15px;height:15px;cursor:pointer;accent-color:var(--brand);}

.seg{display:inline-flex;background:#F1F5F9;border-radius:9px;padding:3px;gap:2px;}
.seg button{border:0;background:transparent;padding:5px 11px;border-radius:7px;font-size:12.5px;color:var(--muted);cursor:pointer;}
.seg button.active{background:#fff;color:var(--text);box-shadow:var(--shadow-sm);font-weight:550;}

/* ── 模态 / 提示 ────────────────────────────────────── */
.mask{position:fixed;inset:0;background:rgba(15,23,42,.45);display:grid;place-items:center;z-index:50;padding:20px;}
.modal{background:#fff;border-radius:16px;width:min(640px,100%);max-height:88vh;display:flex;flex-direction:column;box-shadow:0 24px 60px rgba(15,23,42,.25);overflow:hidden;}
.modal-head{padding:18px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;flex:0 0 auto;}
/* min-height:0 必须加：否则 flex 子项会被内容撑高，把底部按钮推出可视区 */
.modal-body{padding:18px 20px;overflow-y:auto;flex:1 1 auto;min-height:0;}
.modal-body .textarea{max-height:38vh;}
.modal-foot{padding:14px 20px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;gap:8px;flex:0 0 auto;background:#fff;}
.toast-wrap{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:8px;z-index:60;}
.toast{background:var(--navy);color:#fff;padding:10px 14px;border-radius:10px;font-size:13px;box-shadow:var(--shadow);max-width:360px;}
.toast.ok{background:#065F46;}
.toast.err{background:#991B1B;}
.toast.info{background:var(--navy);}
.banner{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:10px;font-size:13px;margin-bottom:14px;}
.banner.warn{background:#FFFBEB;border:1px solid #FDE68A;color:#92400E;}
.banner.err{background:var(--red-bg);border:1px solid #FECACA;color:#991B1B;}
.banner.info{background:var(--blue-bg);border:1px solid #BFDBFE;color:#1E40AF;}

/* ── 移动端 ─────────────────────────────────────────── */
.mobile-nav{display:none;}
@media (max-width:1100px){
  .quick-grid,.stat-grid{grid-template-columns:repeat(2,1fr);}
  .cols{grid-template-columns:1fr;}
  .meeting-overview{grid-template-columns:repeat(2,1fr);}
  .chat-sessions{width:200px;flex-basis:200px;}
}
@media (max-width:820px){
  .app{flex-direction:column;}
  .sidebar{width:100%;flex:0 0 auto;flex-direction:row;align-items:center;padding:10px 12px;gap:12px;}
  .brand{padding:0;}
  .brand-sub{display:none;}
  .nav{flex-direction:row;gap:4px;overflow-x:auto;flex:1;}
  .nav-item span.label{display:none;}
  .nav-item{padding:8px;width:auto;}
  .sidebar-foot{display:none;}
  .topbar{padding:12px 16px;}
  .content{padding:16px;}
  .split{grid-template-columns:1fr;}
  .panel-list{max-height:none;}
  .chat-sessions{display:none;}
  .chat-composer{padding:10px 14px 14px;}
  .quick-grid,.stat-grid{grid-template-columns:1fr;}
  .meeting-overview{grid-template-columns:1fr;}
  .hero{padding:20px;}
  .hero h2{font-size:20px;}
}
"""
