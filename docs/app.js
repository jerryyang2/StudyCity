"use strict";

// 認證權杖只留在此分頁記憶體；同源服務使用 HttpOnly Cookie。
const $ = (id) => document.getElementById(id);
const state = { user: null, groups: [], detail: null, groupId: null, tab: "tasks", token: null, busy: false, api: null, ready: false };
const roleNames = { teacher: "老師", parent: "家長", student: "學生" };
const redemptionRequests = new Map();
let toastTimer;

function node(tag, cls, ...children) {
  const element = document.createElement(tag);
  if (cls) element.className = cls;
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}
function button(label, action, cls = "secondary", disabled = false) {
  const element = node("button", `button ${cls}`, label);
  element.type = "button"; element.disabled = disabled;
  element.addEventListener("click", action); return element;
}
function textButton(label, action) { return button(label, action, "soft small"); }
function notice(message, warning = false) { return node("div", `notice${warning ? " warning" : ""}`, message); }
function toast(message, error = false) {
  clearTimeout(toastTimer); $("toast").textContent = message;
  $("toast").className = `toast${error ? " error" : ""}`; $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6500);
}
function safeBase(value) {
  const url = new URL(value);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname) || url.hostname.endsWith(".local");
  if (url.username || url.password || url.search || url.hash || (url.pathname !== "/" && url.pathname !== "") ||
      (url.protocol !== "https:" && !(local && url.protocol === "http:"))) throw new Error("請使用有效的 HTTPS 服務網址，本機驗收可使用 localhost。 ");
  return url.origin;
}
function configuredBase() {
  let saved;
  try { saved = localStorage.getItem("studycity.apiBase"); } catch { /* 私密瀏覽可不保存非機密設定。 */ }
  const configured = saved || window.STUDYCITY_CONFIG?.apiBase;
  if (configured) return safeBase(configured);
  if (["localhost", "127.0.0.1", "[::1]"].includes(location.hostname) || location.hostname.endsWith(".local")) return location.origin;
  return null;
}
function sameOrigin() { return state.api === location.origin; }
async function api(path, data) {
  if (!state.api) throw new Error("教室服務尚未設定。網站可先瀏覽；註冊及教室資料需連線到教室服務後使用。");
  const headers = { Accept: "application/json", "X-StudyCity-Web": sameOrigin() ? "1" : "token" };
  if (data !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let response;
  try {
    response = await fetch(state.api + "/" + path, { method: data === undefined ? "GET" : "POST", headers,
      credentials: sameOrigin() ? "same-origin" : "omit", body: data === undefined ? undefined : JSON.stringify(data),
      cache: "no-store", redirect: "error", signal: AbortSignal.timeout(20000) });
  } catch { throw new Error("連線尚未完成。請確認服務與網路，重新整理查看目前紀錄後再試。"); }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "auth/login" && path !== "auth/register" && path !== "auth/delete") clearUser();
    throw new Error(typeof result.message === "string" ? result.message.slice(0, 200) : "服務未能完成這次操作。");
  }
  return result;
}
function clearUser() {
  state.user = null; state.groups = []; state.groupId = null; state.detail = null; state.token = null;
  redemptionRequests.clear(); render();
}
async function refresh() {
  const result = await api("me"); state.user = result.user; state.groups = result.groups;
  if (state.groupId) {
    if (state.groups.some((group) => group.id === state.groupId)) state.detail = await api(`groups/${state.groupId}`);
    else { state.groupId = null; state.detail = null; }
  }
  render();
}
async function run(action, message) {
  if (state.busy) return false;
  state.busy = true; document.body.setAttribute("aria-busy", "true");
  try { await action(); if (message) toast(message); return true; }
  catch (error) { toast(error.message || "操作尚未完成，請再試一次。", true); return false; }
  finally { state.busy = false; document.body.removeAttribute("aria-busy"); }
}
async function mutation(path, data = {}, message) {
  return run(async () => { await api(path, data); await refresh(); }, message);
}
function field(label, name, options = {}) {
  const wrapper = node("label", "field", node("span", "", label));
  let input;
  if (options.multiline) { input = document.createElement("textarea"); input.rows = 4; }
  else if (options.choices) {
    input = document.createElement("select");
    for (const [value, title] of options.choices) { const option = node("option", "", title); option.value = value; input.append(option); }
  } else { input = document.createElement("input"); input.type = options.type || "text"; }
  input.name = name; input.required = options.required !== false;
  for (const key of ["placeholder", "min", "max", "minLength", "maxLength", "pattern", "autocomplete"]) {
    if (options[key] !== undefined) input[key] = options[key];
  }
  if (options.value !== undefined) input.value = options.value;
  if (options.type === "password") input.spellcheck = false;
  wrapper.append(input); if (options.help) wrapper.append(node("small", "", options.help));
  return wrapper;
}
function checkbox(label, name, checked = false) {
  const input = document.createElement("input"); input.type = "checkbox"; input.name = name; input.checked = checked;
  return node("label", "toggle-row", input, node("span", "", label));
}
function closeDialog() { if (!state.busy) { $("modal").close(); $("dialog-content").replaceChildren(); } }
function openDialog(title, content) {
  $("dialog-title").textContent = title; $("dialog-content").replaceChildren(content);
  if (!$("modal").open) $("modal").showModal();
}
function formDialog(title, description, fields, submitTitle, action, { danger = false, disabled = false, extra = null } = {}) {
  const form = node("form", "");
  if (description) form.append(node("p", "dialog-description", description));
  if (extra) form.append(extra);
  form.append(...fields);
  const errorBox = node("p", "form-error"); errorBox.setAttribute("role", "alert"); form.append(errorBox);
  const submit = node("button", `button ${danger ? "danger" : "primary"}`, submitTitle); submit.type = "submit"; submit.disabled = disabled;
  form.append(node("div", "form-actions", button("取消", closeDialog), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.busy || !form.reportValidity()) return;
    errorBox.textContent = ""; submit.disabled = true; const original = submit.textContent; submit.textContent = "處理中…";
    state.busy = true;
    try { await action(new FormData(form)); state.busy = false; closeDialog(); }
    catch (error) { errorBox.textContent = error.message || "操作未完成，請重試。"; }
    finally { state.busy = false; submit.disabled = disabled; submit.textContent = original; }
  });
  openDialog(title, form); return form;
}

function authDialog(register = true, role = "student") {
  const switcher = node("div", "auth-switch");
  const loginTab = button("登入", () => authDialog(false, role), register ? "" : "active");
  const registerTab = button("建立帳號", () => authDialog(true, role), register ? "active" : "");
  loginTab.className = register ? "" : "active"; registerTab.className = register ? "active" : "";
  switcher.append(loginTab, registerTab);
  const fields = [];
  if (!state.ready) fields.push(notice("教室服務尚未連線。完成服務設定後，就能註冊、建立教室並儲存資料。", true));
  if (register) {
    fields.push(field("我要以什麼身分使用？", "role", { choices: [["student", "學生，加入教室"], ["parent", "家長，建立家庭"]], value: role === "parent" ? "parent" : "student" }));
    fields.push(field("顯示代稱", "alias", { maxLength: 30, placeholder: "使用大家認得的暱稱", autocomplete: "nickname", help: "不需要填真實姓名。" }));
  }
  fields.push(field("登入帳號", "username", { minLength: 3, maxLength: 32, pattern: "[a-z0-9_]{3,32}", placeholder: "3–32 個小寫英文、數字或底線", autocomplete: "username" }));
  fields.push(field("密碼", "password", { type: "password", minLength: register ? 10 : 1, maxLength: 128, autocomplete: register ? "new-password" : "current-password", help: register ? "至少 10 個字元，建議使用不與其他服務共用的密碼。" : undefined }));
  if (register) fields.push(field("再次確認密碼", "confirm", { type: "password", minLength: 10, maxLength: 128, autocomplete: "new-password" }));
  if (sameOrigin()) fields.push(checkbox("在這個瀏覽器保持登入，最多 7 天", "remember"));
  const description = register ? "學生註冊後以邀請碼加入教室。家長可建立家庭群組；老師帳號請先填寫私密開通申請，由管理者確認。" : "歡迎回來。老師請使用管理者提供的帳號登入，即可建立教室。";
  const form = formDialog(register ? "開始你的 StudyCity 教室" : "歡迎回來", description, fields,
    register ? "建立帳號" : "登入教室", async (data) => {
      if (register && data.get("password") !== data.get("confirm")) throw new Error("兩次密碼不一致，請重新確認。");
      const payload = { username: data.get("username"), password: data.get("password"), remember: data.has("remember") };
      if (register) Object.assign(payload, { alias: data.get("alias"), role: data.get("role") });
      const result = await api(register ? "auth/register" : "auth/login", payload);
      state.token = result.token || null; state.user = result.user; await refresh();
      toast(register ? "帳號已建立，歡迎加入。" : "已登入教室。");
    }, { disabled: !state.ready, extra: switcher });
  form.append(node("p", "auth-separator", sameOrigin() ? "登入狀態以 HttpOnly Cookie 保護，密碼不保存在網頁。" : "登入只保留在這個分頁，關閉或重新整理後需再次登入。"));
  form.append(button("我是老師，申請帳號開通", teacherTicketDialog, "soft full-width"));
}
function teacherTicketDialog() {
  const content = node("div", "", node("p", "dialog-description", "老師帳號由管理者人工確認開通。申請資料僅供教師身分核對與聯繫，不公開在 GitHub。開通後，你可自行管理教室、任務、商品與獎勵。"));
  const configured = window.STUDYCITY_CONFIG?.teacherApplicationURL;
  let url;
  try { url = configured ? new URL(configured) : null; } catch { url = null; }
  if (url && url.protocol === "https:" && url.hostname === "docs.google.com" && url.pathname.startsWith("/forms/")) {
    url.searchParams.set("embedded", "true");
    const frame = document.createElement("iframe"); frame.src = url.href; frame.className = "teacher-application";
    frame.title = "StudyCity 老師帳號開通申請表"; frame.referrerPolicy = "no-referrer";
    content.append(frame);
    const link = node("a", "text-button", "無法顯示時，在新分頁填寫");
    link.href = configured; link.target = "_blank"; link.rel = "noopener noreferrer"; content.append(link);
  } else {
    content.append(notice("私密申請表正在設定中，目前還沒有收件。將收集姓名／稱呼、學校、職務、工作信箱、使用目的與規模，不要求身分證或學生資料。", true));
  }
  content.append(button("已有老師帳號，登入", () => authDialog(false), "secondary full-width"));
  openDialog("老師帳號開通申請", content);
}
function createGroupDialog() {
  const family = state.user.role === "parent";
  formDialog(family ? "建立一個家庭群組" : "建立一個新教室", "取一個大家認得的名字。建立後會產生專屬邀請碼，讓學生加入。",
    [field(family ? "家庭群組名稱" : "教室名稱", "name", { maxLength: 50, placeholder: family ? "例如：我們家的小小計畫" : "例如：一起前進學習班" })], "建立並取得邀請碼", async (data) => {
      const result = await api("groups", { name: data.get("name") }); state.groupId = result.id; state.tab = "tasks";
      await refresh(); toast("建立完成，邀請碼已準備好。");
    });
}
function joinDialog() {
  formDialog("加入一個教室", "向老師或家長取得邀請碼。加入後，只會分享你在此教室提交的任務與積分紀錄。",
    [field("教室邀請碼", "invite", { maxLength: 100, placeholder: "貼上老師或家長提供的邀請碼", autocomplete: "off" })], "確認加入", async (data) => {
      const result = await api("groups/join", { invite: data.get("invite") }); state.groupId = result.id; state.tab = "tasks";
      await refresh(); toast("已加入教室。");
    });
}
function itemDialog(product = false, existing = null) {
  const fields = [field(product ? "獎勵名稱" : "任務名稱", "title", { maxLength: 80, value: existing?.title || "" }),
    field(product ? "兌換價格（教室積分）" : "完成獎勵（教室積分）", "points", { type: "number", min: 1, max: product ? 10000 : 1000, value: existing?.price || 10 })];
  if (product) fields.push(field("商品內容與領取說明", "description", { multiline: true, required: false, maxLength: 1000, value: existing?.description || "" }),
    field("剩餘庫存（留空表示不限量）", "stock", { type: "number", required: false, min: 0, max: 1000000, value: existing?.stock ?? "" }),
    checkbox("上架，讓學生可以兌換", "active", existing ? existing.active === 1 : true));
  if (!product) fields.push(field("截止時間（選填）", "due", { type: "datetime-local", required: false }));
  formDialog(product ? existing ? "編輯商城獎勵" : "在商城上架一份獎勵" : "發布一個小任務", product ? "由你決定獎勵內容、價格、庫存及是否上架。修改不會改變先前兌換的商品與價格紀錄。" : "把目標拆成能做到的小步驟。學生提交後，由你確認並核發積分。", fields, product ? existing ? "儲存商品" : "新增獎勵" : "發布任務", async (data) => {
    const payload = { title: data.get("title"), [product ? "price" : "points"]: Number(data.get("points")) };
    if (!product && data.get("due")) payload.due_at = new Date(String(data.get("due"))).getTime() / 1000;
    if (product) Object.assign(payload, { description: data.get("description"), stock: data.get("stock") === "" ? null : Number(data.get("stock")), active: data.has("active") });
    await api(existing ? `products/${existing.id}/update` : `groups/${state.groupId}/${product ? "products" : "tasks"}`, payload); await refresh();
    toast(product ? "獎勵已上架。" : "任務已發布。");
  });
}
function confirmDialog(title, description, label, action, danger = false) {
  formDialog(title, description, [], label, async () => { await action(); await refresh(); }, { danger });
}
function archive(kind, id) {
  confirmDialog("將這個項目下架？", "下架後不再接受新的提交或兌換，既有審核、積分與領取紀錄會保留。", "確認下架",
    () => api(`${kind}/${id}/archive`, {}), true);
}
function redeem(product) {
  confirmDialog("把努力，換成喜歡的獎勵", `使用 ${product.price} 教室積分兌換「${product.title}」。確認後立即扣除積分，由群組建立者提供並確認領取。`, "確認兌換", async () => {
    const key = redemptionRequests.get(product.id) || crypto.randomUUID(); redemptionRequests.set(product.id, key);
    await api(`products/${product.id}/redeem`, { request_id: key }); redemptionRequests.delete(product.id); toast("兌換成功，可在領取紀錄查看。");
  });
}
function groupButton(group) {
  const element = node("button", `side-button${state.groupId === group.id ? " active" : ""}`, group.name);
  element.type = "button";
  element.addEventListener("click", () => run(async () => { state.groupId = group.id; state.tab = "tasks"; await refresh(); }));
  return element;
}
function render() {
  $("landing").hidden = !!state.user; $("workspace").hidden = !state.user;
  const nav = $("account-nav"); nav.replaceChildren();
  if (!state.user) {
    nav.append(button("登入", () => authDialog(false), "secondary small"), button("建立帳號", () => authDialog(true), "primary small")); return;
  }
  nav.append(node("span", "role-chip", roleNames[state.user.role] || "成員"), button("登出", logout, "secondary small"));
  const sidebar = $("sidebar-content"); sidebar.replaceChildren();
  sidebar.append(node("div", "sidebar-user", node("strong", "", state.user.alias), node("span", "muted", "今天，也照自己的步調前進。")));
  const navigation = node("nav", "");
  const overviewButton = node("button", `side-button${state.groupId === null ? " active" : ""}`, "我的教室");
  overviewButton.type = "button"; overviewButton.addEventListener("click", () => { state.groupId = null; state.detail = null; render(); });
  navigation.append(overviewButton, node("p", "sidebar-label", "已加入的群組"), ...state.groups.map(groupButton)); sidebar.append(navigation);
  sidebar.append(node("p", "sidebar-label", "帳號"), button("帳號與資料", accountDialog, "soft small"));
  const content = $("workspace-content"); content.replaceChildren();
  if (state.groupId && state.detail) renderGroup(content); else renderOverview(content);
}
function heading(title, subtitle, action) { return node("div", "page-heading", node("div", "", node("h1", "", title), node("p", "muted", subtitle)), action || null); }
function stat(value, label) { return node("div", "stat", node("strong", "", value), node("span", "", label)); }
function empty(title, description, action) { return node("div", "empty-state", node("h2", "", title), node("p", "", description), action || null); }
function renderOverview(content) {
  const student = state.user.role === "student";
  content.append(heading(`${state.user.alias}，你好。`, student ? "一件小事，一份看得見的進步。" : "今天，想和學生一起完成什麼？",
    button(student ? "加入教室" : state.user.role === "parent" ? "建立家庭群組" : "建立新教室", student ? joinDialog : createGroupDialog, "primary")));
  content.append(node("div", "stats", stat(state.groups.length, "我的群組"),
    stat(state.groups.reduce((sum, group) => sum + (group.task_count || 0), 0), "群組任務"), stat(student ? "自己決定" : "正向鼓勵", student ? "是否參加排行" : "一起前進的方式")));
  if (!state.groups.length) {
    content.append(empty(student ? "找到一起努力的夥伴" : "第一個教室，從這裡開始", student ? "準備好老師或家長給你的邀請碼，加入後就能查看任務與獎勵。" : "建立教室、分享邀請碼，再發布一件能完成的小任務。",
      button(student ? "輸入邀請碼" : "建立我的第一個群組", student ? joinDialog : createGroupDialog, "primary"))); return;
  }
  const cards = node("div", "card-grid");
  for (const group of state.groups) {
    cards.append(node("article", "class-card", node("span", "class-symbol", group.kind === "family" ? "⌂" : "▤"),
      node("h2", "", group.name), node("p", "muted", `${group.member_count || 0} 位學生 · ${group.task_count || 0} 項任務`),
      button("進入教室", () => run(async () => { state.groupId = group.id; state.tab = "tasks"; await refresh(); }), "secondary small")));
  }
  content.append(cards);
}
function renderGroup(content) {
  const d = state.detail, owner = d.group.owner_id === state.user.id;
  content.append(heading(d.group.name, owner ? "把每一份努力，變成一起前進的動力。" : "完成任務、累積積分，用自己的步調成長。",
    button("重新整理", () => run(refresh), "secondary small")));
  if (owner && d.group.invite) {
    content.append(node("div", "class-meta", node("span", "", "教室邀請碼"), node("code", "invite-code", d.group.invite),
      textButton("複製邀請碼", async () => { try { await navigator.clipboard.writeText(d.group.invite); toast("邀請碼已複製。"); } catch { toast("請選取邀請碼後複製。"); } }),
      textButton("更換", () => confirmDialog("更換教室邀請碼？", "舊邀請碼將失效，已加入的成員不受影響。", "產生新邀請碼", () => api(`groups/${state.groupId}/preferences`, { rotate_invite: true })))));
  }
  content.append(node("div", "stats", owner ? stat(d.members.length, "位學生成員") : stat(d.wallet, "可用教室積分"),
    stat(owner ? d.submissions.filter((s) => s.status === "pending").length : d.tasks.filter((task) => task.status === "approved").length, owner ? "份待確認努力" : "項已通過任務"), stat(d.products.length, "份商城獎勵")));
  const tabs = node("nav", "panel-tabs"); tabs.setAttribute("aria-label", "教室內容");
  const choices = [["tasks", "任務"], ...(owner ? [["reviews", "確認努力"]] : []), ["shop", "積分商城"], ["redemptions", "領取紀錄"], ["ranking", "自願排行"], ...(!owner ? [["ledger", "積分紀錄"]] : [])];
  for (const [id, label] of choices) {
    const tab = node("button", state.tab === id ? "active" : "", label); tab.type = "button";
    tab.setAttribute("aria-current", state.tab === id ? "page" : "false"); tab.addEventListener("click", () => { state.tab = id; render(); }); tabs.append(tab);
  }
  content.append(tabs);
  ({ tasks: renderTasks, reviews: renderReviews, shop: renderShop, redemptions: renderRedemptions, ranking: renderRanking, ledger: renderLedger }[state.tab] || renderTasks)(content, d, owner);
}
function renderTasks(content, d, owner) {
  content.append(node("div", "section-heading", node("h2", "", "一件一件，慢慢完成"), owner ? button("發布任務", () => itemDialog(false), "primary small") : null));
  if (!d.tasks.length) { content.append(empty("這裡還沒有任務", owner ? "先安排一件能完成的小事，讓學生開始累積成就。" : "老師或家長發布任務後，會出現在這裡。")); return; }
  for (const task of d.tasks) {
    const info = node("div", "", node("h3", "", task.title), node("p", "", `通過確認後 ＋${task.points} 教室積分`));
    if (task.due_at) info.append(node("p", "", `截止 ${new Date(task.due_at * 1000).toLocaleString("zh-TW", { dateStyle: "medium", timeStyle: "short" })}`));
    const actions = node("div", "actions");
    if (owner) actions.append(button("下架", () => archive("tasks", task.id), "secondary small"));
    else if (["open", "rejected"].includes(task.status)) actions.append(button(task.status === "rejected" ? "調整後重新提交" : "我已完成，提交確認", () => mutation(`tasks/${task.id}/submit`, {}, "已提交，等待老師或家長確認。"), "primary small"));
    else actions.append(node("span", `badge ${task.status}`, task.status === "approved" ? "已通過" : "等待確認"));
    content.append(node("article", "list-card", info, actions));
  }
}
function renderReviews(content, d) {
  content.append(node("div", "section-heading", node("h2", "", "看見學生的努力")));
  const pending = d.submissions.filter((s) => s.status === "pending");
  if (!pending.length) content.append(empty("目前沒有待確認的提交", "學生完成並提交後，就能在這裡給予回饋。"));
  for (const submission of pending) {
    content.append(node("article", "list-card", node("div", "", node("h3", "", submission.title), node("p", "", `${submission.alias} · 獎勵 ${submission.points} 教室積分`)),
      node("div", "actions", button(`通過並核發 ＋${submission.points}`, () => mutation(`submissions/${submission.id}/review`, { approved: true }, "已確認並核發積分。"), "primary small"),
        button("請再調整", () => mutation(`submissions/${submission.id}/review`, { approved: false }, "已通知學生可以調整後重新提交。"), "secondary small"))));
  }
  const reviewed = d.submissions.filter((s) => s.status !== "pending");
  if (reviewed.length) {
    content.append(node("h2", "spacer-top", "已確認紀錄"));
    for (const s of reviewed) content.append(node("article", "list-card", node("div", "", node("h3", "", s.title), node("p", "", s.alias)), node("span", `badge ${s.status}`, s.status === "approved" ? "已通過" : "請再調整")));
  }
}
function renderShop(content, d, owner) {
  content.append(node("div", "section-heading", node("h2", "", "把努力，換成喜歡的獎勵"), owner ? button("新增獎勵", () => itemDialog(true), "primary small") : null));
  if (!d.products.length) { content.append(empty("商城還在準備中", owner ? "新增一份能提供的獎勵，讓學生知道努力的方向。" : "群組建立者上架獎勵後，就能用教室積分兌換。")); return; }
  const cards = node("div", "card-grid");
  for (const product of d.products) cards.append(node("article", "reward-card", node("span", "reward-symbol", "✧"), node("h3", "", product.title),
    node("p", "reward-price", `${product.price} 分`), product.description ? node("p", "muted", product.description) : null,
    node("p", "muted", product.stock === null ? "不限量" : `剩餘 ${product.stock} 份`),
    owner ? node("div", "actions", node("span", "badge", product.active === 1 ? "上架中" : "已下架"), button("編輯商品", () => itemDialog(true, product), "secondary small")) : button(product.stock === 0 ? "已兌換完畢" : d.wallet >= product.price ? "兌換獎勵" : `還差 ${product.price - d.wallet} 分`, () => redeem(product), "primary small", product.stock === 0 || d.wallet < product.price)));
  content.append(cards);
}
function renderRedemptions(content, d, owner) {
  content.append(node("div", "section-heading", node("h2", "", "每一份獎勵，好好交到手上")));
  if (!d.redemptions.length) { content.append(empty("目前沒有領取紀錄", "兌換商城獎勵後，會在這裡記錄進度。")); return; }
  for (const r of d.redemptions) content.append(node("article", "list-card", node("div", "", node("h3", "", r.title), node("p", "", `${owner ? r.alias + " · " : ""}${r.price} 教室積分 · ${new Date(r.created_at * 1000).toLocaleDateString("zh-TW")}`)),
    owner && r.status === "pending" ? button("確認已提供獎勵", () => mutation(`redemptions/${r.id}/fulfill`, {}, "已確認領取。"), "primary small") : node("span", `badge ${r.status === "pending" ? "pending" : ""}`, r.status === "fulfilled" ? "已確認領取" : "等待提供獎勵")));
}
function renderRanking(content, d, owner) {
  content.append(node("h2", "section-heading", "自己的進步，也能互相鼓勵"));
  content.append(notice("排行榜需群組開放，且學生自願參加。只在這個群組顯示代稱與累積獲得的積分，兌換獎勵不會讓分數減少。"));
  const toggle = checkbox(owner ? "開放這個教室的排行榜" : "我願意以代稱參加本教室排行榜", "ranking", owner ? d.group.leaderboard : d.members[0]?.ranking === 1);
  toggle.querySelector("input").addEventListener("change", async (event) => {
    const value = event.target.checked;
    if (!await mutation(`groups/${state.groupId}/preferences`, { [owner ? "leaderboard" : "ranking"]: value }, "排行設定已更新。")) render();
  }); content.append(toggle);
  if (!d.ranking.length) { content.append(empty("目前沒有公開排名", "不參加也完全沒關係，完成自己的小目標就很好。")); return; }
  content.append(table(["排行", "代稱", "累積努力積分"], d.ranking.map((row, index) => [index + 1, row.alias, row.points])));
}
function renderLedger(content, d) {
  content.append(node("h2", "section-heading", "我的教室積分紀錄"));
  content.append(notice("顯示最近 200 筆收支。教室積分與 App 私人城市積分分開計算。"));
  if (!d.ledger.length) { content.append(empty("每一份努力，都會留下紀錄", "通過任務確認後，積分會記錄在這裡。")); return; }
  content.append(table(["時間", "項目", "積分"], d.ledger.map((row) => [new Date(row.created_at * 1000).toLocaleString("zh-TW", { dateStyle: "short", timeStyle: "short" }), row.label, row.amount > 0 ? `＋${row.amount}` : row.amount])));
}
function table(headers, rows) {
  const result = node("table", "", node("thead", "", node("tr", "", headers.map((h) => { const th = node("th", "", h); th.scope = "col"; return th; }))), node("tbody", "", rows.map((row) => node("tr", "", row.map((cell) => node("td", "", cell))))));
  return node("div", "table-wrap", result);
}
async function logout() {
  await run(async () => {
    try { await api("auth/logout", {}); clearUser(); toast("已登出，期待下次的小小進步。"); }
    catch (error) {
      if (!sameOrigin()) { clearUser(); throw new Error("已清除此分頁登入；服務端登出尚未確認，原登入最遲七天失效。"); }
      throw error;
    }
  });
}
function accountDialog() {
  const content = node("div", "", node("p", "dialog-description", `${state.user.alias} · ${roleNames[state.user.role]}。帳號 ${state.user.username}。私人城市不會上傳到教室服務。`));
  const actions = node("div", "account-actions");
  if (state.groupId && state.detail?.group.owner_id !== state.user.id) actions.append(button("退出目前教室", () => confirmDialog("退出這個教室？", "退出後無法查看群組資料，既有服務端紀錄仍保留。重新加入需有效邀請碼。", "確認退出", async () => {
    await api(`groups/${state.groupId}/leave`, {}); state.groupId = null; state.detail = null;
  }, true), "secondary"));
  actions.append(button("刪除服務帳號", deleteAccountDialog, "danger")); content.append(actions);
  openDialog("帳號與資料", content);
}
function deleteAccountDialog() {
  const confirm = checkbox("我了解這項操作無法復原", "confirmed"); confirm.querySelector("input").required = true;
  formDialog("刪除教室服務帳號", "此帳號的成員資料、提交、積分與兌換紀錄會刪除。你建立的群組及其中所有任務、商城與其他成員的群組紀錄，也會一併移除。App 私人城市不受影響。",
    [field("輸入目前密碼", "password", { type: "password", maxLength: 128, autocomplete: "current-password" }), confirm], "確認刪除帳號", async (data) => {
      await api("auth/delete", { password: data.get("password") }); clearUser(); toast("服務帳號已刪除。");
    }, { danger: true });
}
function serviceDialog() {
  formDialog("教室服務連線", "本機版本可直接使用這台 Mac 的服務。GitHub Pages 版本需由網站管理者提供 HTTPS 後端，才能跨裝置註冊與加入同一教室。更換服務會清除此分頁登入，請先登出。",
    [field("教室服務網址", "url", { type: "url", value: state.api || "", placeholder: "https://你的教室服務網址", help: "這裡只保存非機密的連線網址，不保存密碼與權杖。" })], "儲存連線設定", async (data) => {
      if (state.user) throw new Error("請先登出目前服務，再更換連線。");
      const base = safeBase(String(data.get("url"))); const old = state.api; state.api = base;
      try { await checkConnection(); } catch (error) { state.api = old; throw error; }
      try { localStorage.setItem("studycity.apiBase", base); } catch { /* 設定可在本分頁使用。 */ }
      render(); toast("教室服務已連線。");
    });
}
async function checkConnection() {
  if (!state.api) { state.ready = false; $("connection").textContent = "教室服務尚未設定"; $("connection").className = "connection warning"; return; }
  const health = await api("health");
  if (health.service !== "StudyCity" || !health.ok) throw new Error("這個網址尚未提供 StudyCity 教室服務。");
  state.ready = true; $("connection").textContent = sameOrigin() ? "教室服務已連線" : "已連線至教室服務"; $("connection").className = "connection";
}

$("hero-register").addEventListener("click", teacherTicketDialog);
$("hero-join").addEventListener("click", () => authDialog(true, "student"));
$("service-settings").addEventListener("click", serviceDialog);
$("close-dialog").addEventListener("click", closeDialog);
$("modal").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
async function boot() {
  render();
  $("local-only-note").hidden = !location.hostname.endsWith("github.io");
  try { state.api = configuredBase(); await checkConnection(); if (state.ready && sameOrigin()) { try { await refresh(); } catch { /* 初次未登入屬正常狀態。 */ } } }
  catch { state.ready = false; $("connection").textContent = "教室服務尚未連線"; $("connection").className = "connection warning"; }
}
boot().then(() => { if (location.hash === "#teacher-apply") teacherTicketDialog(); });
