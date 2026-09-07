/* 企业知识助手 3.0 前端交互逻辑（深色现代控制台 + 多页路由 + CRUD） */
(function () {
    "use strict";

    function getSessionId() {
        return localStorage.getItem("session_id") || "web-" + Date.now();
    }
    function setSessionId(id) {
        localStorage.setItem("session_id", id);
    }
    function newSessionId() {
        var id = "web-" + Date.now();
        setSessionId(id);
        return id;
    }
    var SESSION_ID = getSessionId();
    setSessionId(SESSION_ID);

    const API = {
        chat: "/api/qa/chat", chatStream: "/api/qa/chat/stream", agent: "/api/qa/agent",
        workflowDraft: "/api/qa/workflow/draft", workflowSubmit: "/api/qa/workflow/submit",
        clear: "/api/qa/clear", upload: "/api/upload", ingest: "/api/knowledge/ingest",
        list: "/api/knowledge/list", stats: "/api/knowledge/stats", knowledgeDoc: "/api/knowledge/doc/",
        workflowDoc: "/api/qa/workflow/",
        modelConfig: "/api/model/config",
        modelTest: "/api/model/test", modelModels: "/api/model/models",
        kbList: "/api/kb/list", kbCreate: "/api/kb/create",
        kbDelete: "/api/kb/", kbDepts: "/api/kb/", authUsers: "/api/auth/users",
        authRegister: "/api/auth/register", authUpdate: "/api/auth/", authMe: "/api/auth/me",
        feedback: "/api/qa/feedback", feedbackList: "/api/qa/feedback/list",
        feedbackResolve: "/api/qa/feedback/", sysStats: "/api/qa/stats",
        sessions: "/api/chat/sessions", sessionMessages: "/api/chat/sessions/",
        sessionFollowups: "/api/chat/sessions/", historySearch: "/api/chat/search",
    };

    /* ========== 工具函数 ========== */
    function $(id) { return document.getElementById(id); }
    function esc(str) {
        return String(str == null ? "" : str).replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    /* ========== 认证：JWT 存储与请求自动带 Token ========== */
    const AUTH = {
        getToken() { return localStorage.getItem("jwt") || ""; },
        getUser() { try { return JSON.parse(localStorage.getItem("user") || "null"); } catch (e) { return null; } },
        setAuth(token, user) {
            localStorage.setItem("jwt", token);
            localStorage.setItem("user", JSON.stringify(user));
            renderUserArea();
            refreshAfterAuth();
        },
        clear() {
            localStorage.removeItem("jwt");
            localStorage.removeItem("user");
            renderUserArea();
            refreshAfterAuth();
        },
        isLoggedIn() { return !!this.getToken(); },
    };

    /* 登录/退出后刷新状态与数据 */
    function refreshAfterAuth() {
        if (typeof loadStats === "function") loadStats();
        if (typeof loadDashboard === "function") loadDashboard();
        if (typeof loadQuickModel === "function") loadQuickModel();
    }

    /* 轻提示（自动消失） */
    function showToast(msg) {
        const t = document.createElement("div");
        t.className = "toast";
        t.textContent = msg;
        document.body.appendChild(t);
        setTimeout(function () { t.remove(); }, 2500);
    }

    /* ========== 顶栏模型快捷切换器（v2.4） ========== */
    let QUICK_MODEL_CFG = null;
    async function loadQuickModel() {
        const sel = $("quickModelSelect");
        if (!sel) return;
        try {
            const cfg = await json(API.modelConfig);
            QUICK_MODEL_CFG = cfg;
            const providers = cfg.providers || {};
            let html = "";
            Object.keys(providers).forEach(function (pk) {
                const models = providers[pk].models || [];
                if (!models.length) return;
                html += '<optgroup label="' + esc(providers[pk].name) + '">' +
                    models.map(function (m) {
                        const cur = cfg.provider === pk && cfg.model === m;
                        return '<option value="' + pk + '|' + esc(m) + '"' + (cur ? " selected" : "") + '>' + esc(m) + '</option>';
                    }).join("") + '</optgroup>';
            });
            const preset = cfg.provider && providers[cfg.provider];
            if (cfg.model && !(preset && (preset.models || []).indexOf(cfg.model) !== -1)) {
                html = '<optgroup label="当前使用">' +
                    '<option value="' + esc(cfg.provider || "") + '|' + esc(cfg.model) + '" selected>' + esc(cfg.model) + '（自定义）</option>' +
                    '</optgroup>' + html;
            }
            html += '<option value="__settings__">⚙ 模型设置 / 换服务商…</option>';
            sel.innerHTML = html;
        } catch (e) {
            sel.innerHTML = '<option value="">模型加载失败</option>';
        }
        const isAdmin = AUTH.getUser() && AUTH.getUser().role === "admin";
        sel.disabled = !isAdmin;
        sel.title = isAdmin ? "切换问答使用的模型（保存后立即生效）" : "当前模型（仅管理员可切换）";
    }
    function setupQuickModelSwitcher() {
        const sel = $("quickModelSelect");
        if (!sel) return;
        sel.addEventListener("change", async function () {
            const v = sel.value;
            if (v === "__settings__") { switchPage("model"); loadQuickModel(); return; }
            if (!v) return;
            const sep = v.indexOf("|");
            const provider = v.slice(0, sep), model = v.slice(sep + 1);
            const cfg = QUICK_MODEL_CFG || {};
            if (cfg.provider && provider && cfg.provider !== provider) {
                // 跨服务商：Key 不通用，引导到设置页配置并测试
                if (confirm("切换到「" + provider + "」需要该服务商自己的 API Key。\n\n点击「确定」前往模型设置页配置 Key 并测试连接。")) {
                    switchPage("model");
                }
                loadQuickModel();
                return;
            }
            sel.disabled = true;
            try {
                await json(API.modelConfig, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ provider: provider || null, model: model }),
                });
                showToast("已切换到 " + model + "，后续回答立即生效");
                loadQuickModel();
            } catch (e) {
                alert("切换失败：" + e.message);
                loadQuickModel();
            }
            sel.disabled = false;
        });
    }

    async function json(url, opts) {
        opts = opts || {};
        opts.headers = opts.headers || {};
        const tok = AUTH.getToken();
        if (tok) opts.headers["Authorization"] = "Bearer " + tok;
        if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(opts.body);
        }
        const res = await fetch(url, opts);
        if (res.status === 401 && AUTH.isLoggedIn()) {
            AUTH.clear();
            showLoginModal("登录已过期，请重新登录");
        }
        if (!res.ok) {
            let msg = "HTTP " + res.status;
            try { const e = await res.json(); msg = e.detail || msg; } catch (x) { /* ignore */ }
            throw new Error(msg);
        }
        return res.json();
    }

    /* ========== 用户区渲染（侧边栏底部） ========== */
    function renderUserArea() {
        const user = AUTH.getUser();
        const card = document.querySelector(".user-card");
        const avatar = $("userAvatar"), name = $("userName"), role = $("userRole"), action = $("userActionBtn");
        if (user) {
            card.classList.remove("user-card-guest");
            avatar.textContent = (user.username || "客").charAt(0).toUpperCase();
            name.textContent = user.username;
            const roleMap = { admin: "管理员", user: "用户" };
            role.textContent = roleMap[user.role] || user.role || "用户";
            action.textContent = "⏻";
            action.title = "退出登录";
            action.onclick = function (e) {
                e.stopPropagation();
                if (!confirm("确认退出登录？")) return;
                AUTH.clear();
            };
            card.onclick = null;
        } else {
            card.classList.add("user-card-guest");
            avatar.textContent = "客";
            name.textContent = "访客";
            role.textContent = "点击登录";
            action.textContent = "›";
            action.title = "登录";
            action.onclick = function (e) {
                e.stopPropagation();
                showLoginModal();
            };
            // 整张卡片也可点击登录
            card.onclick = function () { showLoginModal(); };
        }
        renderNavPermissions();
    }

    /* 按登录状态与角色控制导航项可见性 */
    function roleLevel(user) {
        if (!user) return "guest";
        return user.role === "admin" ? "admin" : "user";
    }
    function renderNavPermissions() {
        const user = AUTH.getUser();
        const level = roleLevel(user);
        const order = { guest: 0, user: 1, admin: 2 };
        menuItems.forEach(function (item) {
            const need = item.dataset.minRole || "guest";
            if (order[level] >= order[need]) {
                item.style.display = "";
            } else {
                item.style.display = "none";
            }
        });
        // 管理类操作按钮：仅 admin 可见
        const adminBtns = [$("createKbBtn"), $("ingestBtn")];
        adminBtns.forEach(function (b) { if (b) b.style.display = (level === "admin" ? "" : "none"); });
        // 若当前所在页面因权限被隐藏，跳回控制台
        const activeView = document.querySelector(".view.active");
        if (activeView) {
            const navForView = document.querySelector('.nav-item[data-view="' + activeView.id.replace("view-", "") + '"]');
            if (navForView && navForView.style.display === "none") {
                switchPage("dashboard");
            }
        }
    }

    function showLoginModal(preFillMsg) {
        const notice = preFillMsg ? '<div class="result-msg err" style="margin-bottom:10px;">' + esc(preFillMsg) + '</div>' : "";
        openModal("登录", notice +
            '<div class="form-group"><label>用户名</label><input type="text" id="loginUsername" autocomplete="username"></div>' +
            '<div class="form-group"><label>密码</label><input type="password" id="loginPassword" autocomplete="current-password"></div>' +
            '<div class="form-hint">没有账号？<a href="javascript:void(0)" id="goRegister">注册一个</a></div>',
            function (close) {
                const u = $("loginUsername").value.trim(), p = $("loginPassword").value;
                if (!u || !p) { alert("用户名和密码不能为空"); return false; }
                json(API.authMe.replace("/me", "/login"), {
                    method: "POST", body: { username: u, password: p },
                }).then(function (data) {
                    AUTH.setAuth(data.token, data.user);
                    close();
                }).catch(function (e) { alert("登录失败：" + e.message); });
                return false;
            });
        const goReg = document.getElementById("goRegister");
        if (goReg) goReg.addEventListener("click", function () {
            modalOverlay.style.display = "none";
            $("modalOk").onclick = null;
            showRegisterModal();
        });
    }

    function showRegisterModal() {
        openModal("注册新用户",
            '<div class="form-group"><label>用户名</label><input type="text" id="regUsername"></div>' +
            '<div class="form-group"><label>密码</label><input type="password" id="regPassword"></div>' +
            '<div class="form-group"><label>部门</label><input type="text" id="regDept" placeholder="如：hr"></div>',
            function (close) {
                const u = $("regUsername").value.trim(), p = $("regPassword").value;
                if (!u || !p) { alert("用户名和密码不能为空"); return false; }
                json(API.authRegister, {
                    method: "POST",
                    body: { username: u, password: p, department: $("regDept").value.trim(), role: "user" },
                }).then(function (data) {
                    AUTH.setAuth(data.token, data.user);
                    close();
                    alert("注册成功，已自动登录。\n你的账号默认权限为「普通用户」，如需管理权限（上传文档/管理知识库/用户），请联系管理员开通。");
                }).catch(function (e) { alert("注册失败：" + e.message); });
                return false;
            });
    }

    /* ========== 路由：侧边栏切换页面 ========== */
    const sidebar = $("sidebar");
    if (localStorage.getItem("sidebar_collapsed") === "1") sidebar.classList.add("collapsed");
    function bindCollapse(btn) {
        btn.addEventListener("click", function () {
            const c = sidebar.classList.toggle("collapsed");
            localStorage.setItem("sidebar_collapsed", c ? "1" : "0");
        });
    }
    // 每个视图顶部的折叠按钮都联动同一个 sidebar
    ["sidebarToggle", "sidebarToggle2", "sidebarToggle3", "sidebarToggle4", "sidebarToggle5", "sidebarToggle6"]
        .forEach(function (id) { const b = $(id); if (b) bindCollapse(b); });

    const menuItems = document.querySelectorAll(".nav-item[data-view]");
    function switchPage(page) {
        menuItems.forEach(function (i) {
            i.classList.toggle("active", i.dataset.view === page);
        });
        document.querySelectorAll(".view").forEach(function (p) {
            p.classList.toggle("active", p.id === "view-" + page);
        });
        // 进入页面时加载数据
        if (page === "dashboard") loadDashboard();
        else if (page === "kb") { loadKbList(); loadDocKbSelect().then(function () { loadDocs(); }); }
        else if (page === "about") { loadAbout(); loadFeedback(); }
        else if (page === "model") loadModelConfig();
        else if (page === "users") loadUsers();
        else if (page === "test") loadTestKbSelect();
    }
    menuItems.forEach(function (item) {
        item.addEventListener("click", function () { switchPage(item.dataset.view); });
    });

    /* ========== 系统测试（仅管理员） ========== */
    // 渲染测试结果块：标题 + 内容（纯文本/JSON 美化）
    function renderTestResult(id, title, content, extra) {
        const el = $(id);
        if (!el) return;
        let html = '<div class="test-result-title">' + esc(title) + '</div>';
        html += '<pre class="test-result-body">' + esc(content) + '</pre>';
        if (extra) html += '<div class="test-result-extra">' + esc(extra) + '</div>';
        el.innerHTML = html;
    }
    // 加载知识库下拉（供隔离检索卡片使用）
    async function loadTestKbSelect() {
        const sel = $("testKbSelect");
        if (!sel) return;
        try {
            const data = await json(API.kbList);
            const kbs = data.knowledge_bases || [];
            sel.innerHTML = kbs.map(function (kb) {
                return '<option value="' + esc(kb.id) + '">' + esc(kb.name) + ' (' + esc(kb.id) + ')</option>';
            }).join("") || '<option value="default">default</option>';
        } catch (e) {
            sel.innerHTML = '<option value="default">default</option>';
        }
    }
    // 意图路由预设问题
    const INTENT_PRESETS = {
        query: "年假怎么申请？需要什么材料？",
        action: "帮我申请3天事假",
        list: "公司有哪些流程可以办理？",
    };
    // ReAct 预设问题
    const REACT_PRESETS = ["查一下请假制度然后帮我发起请假", "公司有哪些流程"];
    // 意图路由测试
    async function runIntentTest() {
        const input = $("testIntentInput");
        const q = (input.value || "").trim();
        if (!q) { alert("请输入测试问题"); return; }
        $("testIntentResult").innerHTML = '<div class="empty-tip">运行中...</div>';
        try {
            const data = await json(API.agent, {
                method: "POST", body: { question: q, session_id: "test-intent" },
            });
            const detail = "intent=" + (data.intent || "-") +
                (data.action ? " | 工单=" + (data.action.ticket_id || "-") : "") +
                (data.workflows ? " | 流程数=" + data.workflows.length : "");
            renderTestResult("testIntentResult", "意图识别结果", data.answer || JSON.stringify(data), detail);
        } catch (e) {
            renderTestResult("testIntentResult", "请求失败", e.message);
        }
    }
    // ReAct 自主工具调用测试
    async function runReactTest() {
        const input = $("testReactInput");
        const q = (input.value || "").trim();
        if (!q) { alert("请输入测试问题"); return; }
        $("testReactResult").innerHTML = '<div class="empty-tip">运行中...</div>';
        try {
            const data = await json(API.agent + "/react", {
                method: "POST", body: { question: q, session_id: "test-react" },
            });
            const trace = (data.trace || []).map(function (t) {
                return "- 工具: " + t.tool + " | 参数: " + JSON.stringify(t.args);
            }).join("\n");
            renderTestResult(
                "testReactResult",
                "回答（mode=" + (data.mode || "-") + "）",
                data.answer || "(无)",
                trace ? "工具调用轨迹：\n" + trace : "未调用工具"
            );
        } catch (e) {
            renderTestResult("testReactResult", "请求失败", e.message);
        }
    }
    // 多轮对话 + 记忆测试（两轮）
    async function runChatTest() {
        const sid = ($("testChatSid").value || "test-admin").trim();
        const q1 = ($("testChatInput1").value || "").trim();
        const q2 = ($("testChatInput2").value || "").trim();
        if (!q1 || !q2) { alert("请填写两轮问题"); return; }
        $("testChatResult").innerHTML = '<div class="empty-tip">运行中...</div>';
        try {
            const r1 = await json(API.chat, {
                method: "POST", body: { question: q1, session_id: sid },
            });
            const r2 = await json(API.chat, {
                method: "POST", body: { question: q2, session_id: sid },
            });
            renderTestResult(
                "testChatResult",
                "两轮对话对比（session=" + sid + "）",
                "第1轮问: " + q1 + "\n第1轮答: " + (r1.answer || "") +
                "\n\n第2轮问: " + q2 + "\n第2轮答: " + (r2.answer || "")
            );
        } catch (e) {
            renderTestResult("testChatResult", "请求失败", e.message);
        }
    }
    // 知识库隔离检索测试
    async function runKbTest() {
        const kb = $("testKbSelect").value || "default";
        const q = ($("testKbInput").value || "").trim();
        if (!q) { alert("请输入检索问题"); return; }
        $("testKbResult").innerHTML = '<div class="empty-tip">运行中...</div>';
        try {
            const data = await json(API.chat, {
                method: "POST", body: { question: q, session_id: "test-kb", kb_id: kb },
            });
            const srcs = (data.sources || []).map(function (s, i) {
                return "[" + (i + 1) + "] " + (s.source || s.doc_id || "(未知来源)");
            }).join("\n");
            renderTestResult(
                "testKbResult",
                "检索结果（kb_id=" + kb + "）",
                data.answer || "(无回答)",
                srcs ? "命中来源：\n" + srcs : "未命中来源"
            );
        } catch (e) {
            renderTestResult("testKbResult", "请求失败", e.message);
        }
    }
    // 绑定测试页事件（元素存在时）
    (function bindTestEvents() {
        const runI = $("testIntentRun"), runR = $("testReactRun"),
            runC = $("testChatRun"), runK = $("testKbRun");
        if (runI) runI.addEventListener("click", runIntentTest);
        if (runR) runR.addEventListener("click", runReactTest);
        if (runC) runC.addEventListener("click", runChatTest);
        if (runK) runK.addEventListener("click", runKbTest);
        // 意图路由预设
        document.querySelectorAll(".test-q").forEach(function (b) {
            b.addEventListener("click", function () {
                const preset = INTENT_PRESETS[b.dataset.t];
                if (preset) { $("testIntentInput").value = preset; runIntentTest(); }
            });
        });
        // ReAct 预设
        document.querySelectorAll(".test-react-q").forEach(function (b, idx) {
            b.addEventListener("click", function () {
                const preset = REACT_PRESETS[idx];
                if (preset) { $("testReactInput").value = preset; runReactTest(); }
            });
        });
        const clearC = $("testChatClear");
        if (clearC) clearC.addEventListener("click", async function () {
            const sid = ($("testChatSid").value || "test-admin").trim();
            try {
                await fetch(API.clear, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ session_id: sid }),
                });
                $("testChatResult").innerHTML = '<span class="empty-tip">已清空会话记忆</span>';
            } catch (e) { alert("清空失败：" + e.message); }
        });
    })();

    /* ========== 通用模态框 ========== */
    const modalOverlay = $("modalOverlay");
    function openModal(title, bodyHtml, onOk) {
        $("modalTitle").textContent = title;
        $("modalBody").innerHTML = bodyHtml;
        modalOverlay.style.display = "flex";
        const okBtn = $("modalOk"), cancelBtn = $("modalCancel"), closeBtn = $("modalClose");
        function close() { modalOverlay.style.display = "none"; okBtn.onclick = null; }
        okBtn.onclick = function () { if (onOk(close) !== false) close(); };
        cancelBtn.onclick = close; closeBtn.onclick = close;
    }

    /* ========== 顶部状态 ========== */
    async function loadStats() {
        try {
            const [stats, model] = await Promise.all([
                json(API.stats), json(API.modelConfig).catch(function () { return {}; }),
            ]);
            $("sysMode").textContent = model.ready ? "生产模式" : "演示模式";
            $("sysMode").className = "badge " + (model.ready ? "badge-prod" : "badge-demo");
            $("vectorStore").textContent = "向量库: " + (stats.vector_store || "-");
            $("modelInfo").textContent = "模型: " + (model.model || stats.llm_model || "-");
        } catch (e) { console.error("状态加载失败", e); }
    }

    /* ========== 仪表盘 ========== */
    async function loadDashboard() {
        try {
            const [stats, model, docs, sysStats] = await Promise.all([
                json(API.stats), json(API.modelConfig).catch(function () { return {}; }),
                json(API.list).catch(function () { return { documents: [] }; }),
                json(API.sysStats).catch(function () { return {}; }),
            ]);
            const docList = docs.documents || [];
            const chunks = docList.reduce(function (s, d) { return s + (d.chunks || 0); }, 0);
            $("statDocs").textContent = docList.length;
            $("statChunks").textContent = chunks;
            $("statVS").textContent = stats.vector_store || "-";
            $("statModel").textContent = model.model || stats.llm_model || "-";
            const wrap = $("dashDocList");
            if (!docList.length) { wrap.innerHTML = '<div class="empty-tip">暂无文档</div>'; }
            else {
                wrap.innerHTML = docList.slice(0, 10).map(function (d) {
                    const name = String(d.source || "").split(/[\\/]/).pop();
                    return '<div class="doc-item"><div class="doc-info"><div class="doc-name">📄 ' + esc(name) +
                        '</div><div class="doc-meta">' + (d.chunks || 0) + ' 个片段</div></div></div>';
                }).join("");
            }

            // v1.5 系统统计
            const cache = sysStats.cache || {};
            const tokens = sysStats.tokens || {};
            const fb = sysStats.feedback || {};
            $("statCacheHit").textContent = Math.round((cache.hit_rate || 0) * 100);
            $("statCacheSize").textContent = "缓存 " + (cache.size || 0) + " 条";
            $("statTokens").textContent = (tokens.total || 0).toLocaleString();
            $("statTokenCalls").textContent = (tokens.call_count || 0) + " 次调用";
            $("statFbScore").textContent = fb.avg_score || "-";
            $("statFbCount").textContent = (fb.total || 0) + " 条反馈";
            $("statFbPending").textContent = fb.pending_count || 0;
            $("statFbRate").textContent = "低分率 " + Math.round((fb.low_score_rate || 0) * 100) + "%";
        } catch (e) { console.error(e); }
    }
    const dashRefreshBtn = $("dashRefreshBtn");
    if (dashRefreshBtn) dashRefreshBtn.addEventListener("click", loadDashboard);

    /* ========== 知识库管理 ========== */
    async function loadKbList() {
        const grid = $("kbGrid");
        try {
            const data = await json(API.kbList);
            const kbs = data.knowledge_bases || [];
            if (!kbs.length) { grid.innerHTML = '<div class="empty-tip">暂无知识库</div>'; return; }
            const isAdmin = AUTH.getUser() && AUTH.getUser().role === "admin";
            grid.innerHTML = kbs.map(function (kb) {
                const isDefault = kb.id === "default";
                const depts = (kb.departments || []);
                const deptTags = depts.length
                    ? depts.map(function (d) { return '<span class="kb-tag">' + esc(d) + '</span>'; }).join("")
                    : '<span class="kb-tag public">公开</span>';
                const delBtn = (isAdmin && !isDefault) ? '<button class="btn-ghost kb-del" data-id="' + esc(kb.id) + '">删除</button>' : "";
                const deptBtn = (isAdmin && !isDefault) ? '<button class="btn-ghost kb-dept" data-id="' + esc(kb.id) + '" data-depts=\'' + JSON.stringify(depts) + '\'>部门</button>' : "";
                const createdAt = kb.created_at ? ('<div class="kb-created">创建于 ' + esc(String(kb.created_at).replace("T", " ").slice(0, 16)) + '</div>') : "";
                return '<div class="kb-card' + (isDefault ? " default" : "") + '">' +
                    '<div class="kb-card-head"><div class="kb-name">' + (isDefault ? "⭐ " : "📚 ") + esc(kb.name) + '</div></div>' +
                    '<div class="kb-id">ID: ' + esc(kb.id) + '</div>' +
                    '<div class="kb-desc">' + esc(kb.description || "无描述") + '</div>' +
                    '<div class="kb-tags">' + deptTags + '</div>' +
                    createdAt +
                    '<div class="kb-actions">' + deptBtn + delBtn + '</div>' +
                    '</div>';
            }).join("");
            grid.querySelectorAll(".kb-del").forEach(function (b) {
                b.addEventListener("click", async function () {
                    if (!confirm("确认删除知识库 " + b.dataset.id + "？")) return;
                    try {
                        await json(API.kbDelete + b.dataset.id, { method: "DELETE" });
                        loadKbList();
                    } catch (e) { alert("删除失败：" + e.message); }
                });
            });
            grid.querySelectorAll(".kb-dept").forEach(function (b) {
                b.addEventListener("click", function () {
                    const cur = JSON.parse(b.dataset.depts || "[]");
                    openModal("设置可访问部门（空=公开）",
                        '<div class="form-group"><label>部门列表（逗号分隔）</label>' +
                        '<input type="text" id="deptInput" value="' + esc(cur.join(",")) + '" placeholder="如：hr,finance"></div>',
                        function (close) {
                            const val = $("deptInput").value.trim();
                            const arr = val ? val.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean) : [];
                            json(API.kbDepts + b.dataset.id + "/departments", {
                                method: "POST", headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ departments: arr }),
                            }).then(function () { close(); loadKbList(); })
                                .catch(function (e) { alert("保存失败：" + e.message); });
                            return false;
                        });
                });
            });
        } catch (e) { grid.innerHTML = '<div class="empty-tip">加载失败：' + esc(e.message) + '</div>'; }
    }

    $("createKbBtn").addEventListener("click", function () {
        openModal("新建知识库",
            '<div class="form-group"><label>知识库 ID</label><input type="text" id="newKbId" placeholder="如：hr"></div>' +
            '<div class="form-group"><label>名称</label><input type="text" id="newKbName" placeholder="如：HR知识库"></div>' +
            '<div class="form-group"><label>描述</label><input type="text" id="newKbDesc"></div>' +
            '<div class="form-group"><label>可访问部门（逗号分隔，空=公开）</label><input type="text" id="newKbDepts" placeholder="如：hr_dept"></div>',
            function (close) {
                const id = $("newKbId").value.trim();
                const name = $("newKbName").value.trim();
                if (!id || !name) { alert("ID 和名称不能为空"); return false; }
                const depts = $("newKbDepts").value.trim();
                const arr = depts ? depts.split(/[,，]/).map(function (s) { return s.trim(); }).filter(Boolean) : [];
                json(API.kbCreate, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ id: id, name: name, description: $("newKbDesc").value, departments: arr }),
                }).then(function () { close(); loadKbList(); })
                    .catch(function (e) { alert("创建失败：" + e.message); });
                return false;
            });
    });
    $("refreshKbBtn").addEventListener("click", loadKbList);

    /* ========== 文档上传/入库/列表（知识库页内） ========== */
    const uploadZone = $("uploadZone"), fileInput = $("fileInput");
    uploadZone.addEventListener("click", function () { fileInput.click(); });
    fileInput.addEventListener("change", function () { if (fileInput.files.length) uploadFiles(fileInput.files); });
    ["dragover", "dragenter"].forEach(function (ev) {
        uploadZone.addEventListener(ev, function (e) { e.preventDefault(); uploadZone.classList.add("dragover"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
        uploadZone.addEventListener(ev, function (e) { e.preventDefault(); uploadZone.classList.remove("dragover"); });
    });
    uploadZone.addEventListener("drop", function (e) {
        if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
    });
    function currentDocKb() {
        const sel = $("docKbSelect");
        return sel ? (sel.value || "default") : "default";
    }
    async function loadDocKbSelect() {
        const sel = $("docKbSelect");
        if (!sel) return;
        try {
            const data = await json(API.kbList);
            const kbs = data.knowledge_bases || [];
            const cur = sel.value || "default";
            sel.innerHTML = kbs.map(function (kb) {
                return '<option value="' + esc(kb.id) + '">' + esc(kb.name) + '</option>';
            }).join("");
            if (Array.from(sel.options).some(function (o) { return o.value === cur; })) sel.value = cur;
        } catch (e) { /* 保留默认 */ }
        sel.addEventListener("change", function () { loadDocs(true); });
    }
    async function uploadFiles(files) {
        const r = $("uploadResult"); r.className = "result-msg"; r.textContent = "上传中...";
        const kb = currentDocKb();
        const results = [];
        let skipped = 0;
        for (const file of files) {
            const form = new FormData(); form.append("file", file); form.append("kb_id", kb);
            try {
                const data = await json(API.upload, { method: "POST", body: form });
                if (data.skipped) skipped++;
                results.push((data.skipped ? "⏭ " : "✅ ") + file.name + " · " + (data.message || ""));
            } catch (e) { results.push("✕ " + file.name + " 失败"); }
        }
        r.classList.add(results.every(function (x) { return x.indexOf("失败") < 0; }) ? "ok" : "err");
        r.textContent = results.join("\n");
        loadDocs(true);
    }
    $("ingestBtn").addEventListener("click", async function () {
        const btn = $("ingestBtn"), r = $("ingestResult");
        btn.disabled = true; r.className = "result-msg"; r.textContent = "入库中...";
        try {
            const data = await json(API.ingest + "?kb_id=" + encodeURIComponent(currentDocKb()), { method: "POST" });
            r.classList.add("ok");
            r.textContent = data.message || "入库完成";
        } catch (e) { r.classList.add("err"); r.textContent = "失败：" + e.message; }
        finally { btn.disabled = false; loadDocs(true); }
    });
    // 文档列表排序切换：按编号 / 按时间
    var sortNameBtn = $("docSortName"), sortTimeBtn = $("docSortTime");
    function applySortBtn() {
        if (sortNameBtn) { sortNameBtn.classList.toggle("active", _docSortMode === "name"); }
        if (sortTimeBtn) { sortTimeBtn.classList.toggle("active", _docSortMode === "time"); }
    }
    if (sortNameBtn) sortNameBtn.addEventListener("click", function () { _docSortMode = "name"; applySortBtn(); loadDocs(true); });
    if (sortTimeBtn) sortTimeBtn.addEventListener("click", function () { _docSortMode = "time"; applySortBtn(); loadDocs(true); });
    applySortBtn();
    let _docsCache = null, _docsCacheTime = 0;
    const DOCS_CACHE_TTL = 5000;
    async function loadDocs(force) {
        const wrap = $("docList");
        const now = Date.now();
        if (!force && _docsCache && (now - _docsCacheTime) < DOCS_CACHE_TTL) {
            renderDocs(_docsCache, wrap);
            return;
        }
        wrap.innerHTML = '<div class="empty-tip">加载中...</div>';
        try {
            const data = await json(API.list + "?kb_id=" + encodeURIComponent(currentDocKb()));
            _docsCache = data;
            _docsCacheTime = now;
            renderDocs(data, wrap);
        } catch (e) {
            wrap.innerHTML = '<div class="empty-tip">加载失败：' + esc(e.message) + '</div>';
        }
    }
    var _docSortMode = "name"; // name(按编号) | time(按更新时间)
    function renderDocs(data, wrap) {
        var all = (data.documents || []);
        // 按编号自然排序（前导数字升序，无编号的排最后）
        function numOf(n) { var m = String(n).match(/^\s*(\d+)/); return m ? parseInt(m[1], 10) : Infinity; }
        var sorted = all.slice().sort(function (a, b) {
            if (_docSortMode === "time") {
                var ta = a.updated_at || a.created_at || "", tb = b.updated_at || b.created_at || "";
                return tb < ta ? -1 : (tb > ta ? 1 : 0);
            }
            var na = numOf(String(a.source || "").split(/[\\/]/).pop());
            var nb = numOf(String(b.source || "").split(/[\\/]/).pop());
            if (na !== nb) return na - nb;
            return String(a.source || "").localeCompare(String(b.source || ""), "zh");
        });
        // 本次更新标记：updated_at 在最近 24h 内即视为「本次更新」
        var nowTs = Date.now(), DAY = 24 * 3600 * 1000;
        var docs = sorted.slice(0, 50);
        var total = data.total || docs.length;
        if (!docs.length) { wrap.innerHTML = '<div class="empty-tip">暂无文档</div>'; return; }
        var isAdmin = AUTH.getUser() && AUTH.getUser().role === "admin";
        var kb = currentDocKb();
        var header = total > docs.length
            ? '<div class="empty-tip" style="padding:6px;">共 ' + total + ' 份文档，显示前 ' + docs.length + ' 份</div>'
            : "";
        // 时间格式化：与钉钉文档列表一致（如 "6月23日 17:05"），当年份与当前不一致时附加年份
        function fmtTime(iso) {
            if (!iso) return "-";
            var d = new Date(iso);
            if (isNaN(d.getTime())) return "-";
            var now = new Date();
            var sameYear = d.getFullYear() === now.getFullYear();
            var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
            if (sameYear) {
                return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + pad(d.getHours()) + ":" + pad(d.getMinutes());
            }
            return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " + pad(d.getHours()) + ":" + pad(d.getMinutes());
        }
        wrap.innerHTML = header + docs.map(function (d) {
            const name = String(d.source || "").split(/[\\/]/).pop();
            const delBtn = isAdmin ? '<button class="doc-del" data-id="' + esc(d.doc_id) + '">✕</button>' : "";
            // 优先显示「最近更新时间」；若从未变更（updated_at 缺失）则回退到 created_at
            const t = d.updated_at || d.created_at;
            const time = fmtTime(t);
            const title = d.created_at && d.updated_at && d.updated_at !== d.created_at
                ? "首次导入：" + fmtTime(d.created_at) + "\n最近更新：" + fmtTime(d.updated_at)
                : "导入时间：" + time;
            // 是否本次更新（最近 24h 内更新过）
            var isNew = false;
            var upTs = t ? new Date(t).getTime() : 0;
            if (upTs && (nowTs - upTs) < DAY) isNew = true;
            var tag = isNew ? '<span class="doc-tag-updated" title="最近 24 小时内更新">♻ 已更新</span>' : "";
            return '<div class="doc-item"><div class="doc-info"><div class="doc-name">📄 ' + esc(name) + tag +
                '</div><div class="doc-meta">' + (d.chunks || 0) + ' 个片段 · ' + esc(time) + '</div></div>' +
                '<span class="doc-time" title="' + esc(title) + '" style="font-size:12px;color:#888;align-self:center;margin-right:6px;">' + esc(time) + '</span>' +
                delBtn + '</div>';
        }).join("");
        wrap.querySelectorAll(".doc-del").forEach(function (b) {
            b.addEventListener("click", async function () {
                if (!confirm("确认删除？")) return;
                try {
                    await json(API.list + "/" + b.dataset.id + "?kb_id=" + encodeURIComponent(kb), { method: "DELETE" });
                    loadDocs(true);
                }
                catch (e) { alert("删除失败：" + e.message); }
            });
        });
    }

    /* ========== 模型设置 ========== */
    let CFG_PROVIDERS = {};
    let CFG_ORIGINAL_PROVIDER = "";
    function fillModelSuggestions(models) {
        $("cfgModelList").innerHTML = (models || []).map(function (m) {
            return '<option value="' + esc(m) + '"></option>';
        }).join("");
    }
    async function loadModelConfig() {
        const isAdmin = AUTH.getUser() && AUTH.getUser().role === "admin";
        try {
            const cfg = await json(API.modelConfig);
            CFG_PROVIDERS = cfg.providers || {};
            const sel = $("cfgProvider");
            sel.innerHTML = '<option value="">自定义</option>' +
                Object.keys(CFG_PROVIDERS).map(function (k) {
                    return '<option value="' + k + '"' + (k === cfg.provider ? " selected" : "") + '>' + esc(CFG_PROVIDERS[k].name) + '</option>';
                }).join("");
            $("cfgBaseUrl").value = cfg.base_url || "";
            $("cfgModel").value = cfg.model || "";
            $("cfgKeyHint").textContent = cfg.api_key_masked ? ("当前：" + cfg.api_key_masked) : "未配置";
            CFG_ORIGINAL_PROVIDER = cfg.provider || "";
            // 当前生效配置展示
            $("cfgActiveHint").textContent = "当前生效：" +
                (CFG_PROVIDERS[cfg.provider] ? CFG_PROVIDERS[cfg.provider].name : (cfg.provider || "自定义")) +
                " / " + (cfg.model || "-") + "　" +
                (cfg.ready ? "✅ 已配置 Key" : "⚠ 演示模式（未配置 Key）");
            // 预置模型填入下拉建议
            const p = CFG_PROVIDERS[cfg.provider];
            fillModelSuggestions(p && p.models);
            sel.onchange = function () {
                const pv = CFG_PROVIDERS[sel.value];
                if (pv) {
                    $("cfgBaseUrl").value = pv.base_url;
                    fillModelSuggestions(pv.models);
                    if (pv.models && pv.models[0]) $("cfgModel").value = pv.models[0];
                    $("cfgModelHint").textContent = "已按服务商预置模型填写，可修改或点「拉取列表」获取全部可用模型";
                } else {
                    fillModelSuggestions([]);
                    $("cfgModelHint").textContent = "自定义服务商：直接输入 base_url 与模型名";
                }
                // 换服务商时：旧 Key 大概率不通用，必须提示填新 Key（防止拿 A 家 Key 调 B 家导致全部回答失败）
                if (sel.value && CFG_ORIGINAL_PROVIDER && sel.value !== CFG_ORIGINAL_PROVIDER && !$("cfgApiKey").value) {
                    $("cfgKeyHint").textContent = "⚠ 已切换服务商：当前保存的是原服务商的 Key，需要填入新服务商的 API Key，测试连接通过后再保存";
                } else if (cfg.api_key_masked) {
                    $("cfgKeyHint").textContent = "当前：" + cfg.api_key_masked;
                }
            };
        } catch (e) { console.error(e); }
        // 非 admin：表单整体只读，仅展示当前配置
        ["cfgProvider", "cfgBaseUrl", "cfgModel", "cfgApiKey", "saveModelBtn", "testModelBtn", "fetchModelsBtn"]
            .forEach(function (id) { $(id).disabled = !isAdmin; });
        $("cfgPermHint").style.display = isAdmin ? "none" : "block";
    }
    function gatherModelConfig() {
        return {
            provider: $("cfgProvider").value, base_url: $("cfgBaseUrl").value,
            model: $("cfgModel").value, api_key: $("cfgApiKey").value,
        };
    }
    $("saveModelBtn").addEventListener("click", async function () {
        const r = $("modelResult"); r.className = "result-msg"; r.textContent = "保存中...";
        try {
            await json(API.modelConfig, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(gatherModelConfig()),
            });
            r.classList.add("ok"); r.textContent = "配置已保存并生效（立即用于问答，无需重启）";
            $("cfgApiKey").value = "";
            loadStats(); loadModelConfig(); loadQuickModel();
        } catch (e) { r.classList.add("err"); r.textContent = "保存失败：" + e.message; }
    });
    $("testModelBtn").addEventListener("click", async function () {
        const r = $("modelResult"); r.className = "result-msg"; r.textContent = "测试中（约几秒）...";
        try {
            const data = await json(API.modelTest, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(gatherModelConfig()),
            });
            r.classList.add(data.ok ? "ok" : "err");
            r.textContent = (data.message || (data.ok ? "连接成功" : "连接失败")) +
                (data.ok ? "　→确认无误请点「保存并生效」" : "");
        } catch (e) { r.classList.add("err"); r.textContent = "测试失败：" + e.message; }
    });
    $("fetchModelsBtn").addEventListener("click", async function () {
        const r = $("modelResult"); r.className = "result-msg"; r.textContent = "拉取模型列表中...";
        try {
            const g = gatherModelConfig();
            const data = await json(API.modelModels, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ provider: g.provider, base_url: g.base_url, api_key: g.api_key }),
            });
            if (data.ok && data.models && data.models.length) {
                fillModelSuggestions(data.models);
                if (!$("cfgModel").value) $("cfgModel").value = data.models[0];
                r.classList.add("ok");
                r.textContent = data.message + "，已填入下拉建议（模型输入框获得焦点或输入时可见）";
            } else {
                r.classList.add("err"); r.textContent = data.message || "未获取到模型";
            }
        } catch (e) { r.classList.add("err"); r.textContent = "拉取失败：" + e.message; }
    });

    /* ========== 用户管理 ========== */
    async function loadUsers() {
        const tbody = $("userTable").querySelector("tbody");
        try {
            const data = await json(API.authUsers).catch(function () { return { users: [] }; });
            const users = data.users || [];
            if (!users.length) { tbody.innerHTML = '<tr><td colspan="6" class="empty-tip">暂无用户（或未启用认证）</td></tr>'; return; }
            tbody.innerHTML = users.map(function (u) {
                return '<tr><td>' + esc(u.username) + '</td>' +
                    '<td><span class="role-tag ' + (u.role || "user") + '">' + esc(u.role) + '</span></td>' +
                    '<td>' + esc(u.department || "-") + '</td>' +
                    '<td>' + esc((u.extra_kbs || []).join(",") || "-") + '</td>' +
                    '<td>' + esc(u.created_at || "-") + '</td>' +
                    '<td><button class="btn-ghost user-edit" data-name="' + esc(u.username) + '">编辑</button></td></tr>';
            }).join("");
            tbody.querySelectorAll(".user-edit").forEach(function (b) {
                b.addEventListener("click", function () { editUser(b.dataset.name); });
            });
        } catch (e) { tbody.innerHTML = '<tr><td colspan="6" class="empty-tip">加载失败</td></tr>'; }
    }
    async function editUser(name) {
        // 拉取用户当前信息和所有知识库，并行
        let user = null, kbs = [];
        try {
            const data = await json(API.authUsers);
            user = (data.users || []).find(function (u) { return u.username === name; }) || null;
        } catch (e) { /* 忽略，后续按空处理 */ }
        try {
            const kbData = await json(API.kbList);
            kbs = kbData.knowledge_bases || [];
        } catch (e) { kbs = []; }

        const cur = new Set((user && user.extra_kbs) || []);
        const role = user && user.role || "user";
        const dept = user && user.department || "";

        const kbChipsHtml = kbs.length
            ? '<div class="kb-picker" id="kbPicker">' + kbs.map(function (kb) {
                const selected = cur.has(kb.id);
                return '<button type="button" class="kb-chip' + (selected ? " selected" : "") + '" data-kb-id="' + esc(kb.id) + '">' +
                    '<span class="kb-chip-dot"></span>' +
                    '<span class="kb-chip-name">' + esc(kb.name || kb.id) + '</span>' +
                    '<span class="kb-chip-id">' + esc(kb.id) + '</span>' +
                    '</button>';
            }).join("") + '</div>' +
                '<div class="kb-picker-hint" id="kbPickerHint"></div>'
            : '<div class="empty-tip">暂无可授权的知识库</div>';

        openModal("编辑用户：" + name,
            '<div class="form-group"><label>部门</label><input type="text" id="editDept" value="' + esc(dept) + '" placeholder="如：hr"></div>' +
            '<div class="form-group"><label>角色</label><select id="editRole"><option value="user"' + (role === "user" ? " selected" : "") + '>user</option><option value="admin"' + (role === "admin" ? " selected" : "") + '>admin</option></select></div>' +
            '<div class="form-group"><label>额外授权知识库（点击切换）</label>' + kbChipsHtml + '</div>' +
            '<input type="hidden" id="editExtra" value="' + esc(Array.from(cur).join(",")) + '">' +
            '<div class="form-group"><label>重置密码（留空则不修改）</label><input type="password" id="editPassword" autocomplete="new-password" placeholder="输入新密码"></div>',
            function (close) {
                // 从 chips 收集选中的 kb id
                const selected = Array.from(document.querySelectorAll("#kbPicker .kb-chip.selected"))
                    .map(function (b) { return b.dataset.kbId; });
                const deptVal = $("editDept").value.trim();
                const roleVal = $("editRole").value;
                const pwdVal = $("editPassword").value;
                // 先保存部门/角色/授权
                json(API.authUpdate + name + "/update", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ department: deptVal, role: roleVal, extra_kbs: selected }),
                }).then(function () {
                    // 填写了密码则继续重置密码
                    if (pwdVal) {
                        return json(API.authUpdate + name + "/reset-password", {
                            method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ new_password: pwdVal }),
                        });
                    }
                    return Promise.resolve();
                }).then(function () { close(); loadUsers(); alert(pwdVal ? "用户信息已更新，密码已重置" : "用户信息已更新"); })
                    .catch(function (e) { alert("更新失败：" + e.message); });
                return false;
            });

        // 绑定 chips 点击切换
        const picker = $("kbPicker");
        if (picker) {
            picker.querySelectorAll(".kb-chip").forEach(function (chip) {
                chip.addEventListener("click", function () {
                    chip.classList.toggle("selected");
                    updateKbPickerHint();
                });
            });
            updateKbPickerHint();
        }
        function updateKbPickerHint() {
            const hint = $("kbPickerHint");
            if (!hint) return;
            const n = document.querySelectorAll("#kbPicker .kb-chip.selected").length;
            hint.textContent = n === 0 ? "未选任何额外授权（用户仍可访问本部门/公开库）" : "已选 " + n + " 个额外授权";
        }
    }
    $("createUserBtn").addEventListener("click", function () {
        openModal("新建用户",
            '<div class="form-group"><label>用户名</label><input type="text" id="newUsername"></div>' +
            '<div class="form-group"><label>密码</label><input type="password" id="newPassword"></div>' +
            '<div class="form-group"><label>角色</label><select id="newRole"><option value="user">user</option><option value="admin">admin</option></select></div>' +
            '<div class="form-group"><label>部门</label><input type="text" id="newDept" placeholder="如：hr"></div>',
            function (close) {
                const u = $("newUsername").value.trim(), p = $("newPassword").value;
                if (!u || !p) { alert("用户名和密码不能为空"); return false; }
                json(API.authRegister, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        username: u, password: p, role: $("newRole").value,
                        department: $("newDept").value.trim(), extra_kbs: [],
                    }),
                }).then(function () { close(); loadUsers(); })
                    .catch(function (e) { alert("创建失败：" + e.message); });
                return false;
            });
    });
    $("refreshUsersBtn").addEventListener("click", loadUsers);

    /* ========== 系统设置页 ========== */
    async function loadAbout() {
        try {
            const [stats, model] = await Promise.all([
                json(API.stats), json(API.modelConfig).catch(function () { return {}; }),
            ]);
            $("aboutMode").textContent = model.ready ? "生产模式" : "演示模式";
            $("aboutVS").textContent = stats.vector_store || "-";
            $("aboutModel").textContent = model.model || stats.llm_model || "-";
        } catch (e) { console.error(e); }
    }

    /* ========== v1.5 用户反馈管理 ========== */
    async function loadFeedback() {
        const tbody = document.querySelector("#fbTable tbody");
        if (!tbody) return;
        const filter = $("fbFilter") ? $("fbFilter").value : "";
        try {
            const url = API.feedbackList + "?limit=50" + (filter ? "&status=" + filter : "");
            const data = await json(url);
            const records = data.records || [];
            if (!records.length) {
                tbody.innerHTML = '<tr><td colspan="5" class="empty-tip">暂无反馈记录</td></tr>';
                return;
            }
            tbody.innerHTML = records.map(function (r) {
                const time = r.timestamp ? new Date(r.timestamp * 1000).toLocaleString("zh-CN") : "-";
                const stars = '<span class="fb-stars">' +
                    [1, 2, 3, 4, 5].map(function (n) {
                        return '<span class="' + (n <= r.score ? "fb-star active" : "fb-star") + '">★</span>';
                    }).join("") + '</span>';
                const status = r.status === "pending"
                    ? '<span class="badge badge-warn">待优化</span>'
                    : '<span class="badge badge-muted">已处理</span>';
                const action = r.status === "pending"
                    ? '<button class="btn-ghost btn-sm fb-resolve" data-id="' + esc(r.id) + '">标记已处理</button>'
                    : '-';
                return '<tr>' +
                    '<td class="fb-question" title="' + esc(r.question || "") + '">' + esc((r.question || "").slice(0, 50)) + '</td>' +
                    '<td>' + stars + '</td>' +
                    '<td>' + status + '</td>' +
                    '<td class="fb-time">' + esc(time) + '</td>' +
                    '<td>' + action + '</td>' +
                    '</tr>';
            }).join("");
            tbody.querySelectorAll(".fb-resolve").forEach(function (btn) {
                btn.addEventListener("click", async function () {
                    try {
                        await json(API.feedbackResolve + btn.dataset.id + "/resolve", { method: "POST" });
                        loadFeedback();
                    } catch (e) { alert("操作失败：" + e.message); }
                });
            });
        } catch (e) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty-tip">加载失败：' + esc(e.message) + '</td></tr>';
        }
    }
    const fbRefreshBtn = $("fbRefreshBtn");
    if (fbRefreshBtn) fbRefreshBtn.addEventListener("click", loadFeedback);
    const fbFilterSel = $("fbFilter");
    if (fbFilterSel) fbFilterSel.addEventListener("change", loadFeedback);

    /* ========== 聊天 ========== */
    const chatBody = $("chatBody"), chatInput = $("chatInput"), sendBtn = $("sendBtn");
    function scrollBottom() { chatBody.scrollTop = chatBody.scrollHeight; }

    /* 轻量 Markdown 渲染：把 AI 回复里的 Markdown 语法转成 HTML（离线、无外部依赖） */
    function mdToHtml(text) {
        if (!text) return "";
        let src = String(text);
        // 1. 先做 HTML 转义，防止注入
        src = esc(src);
        // 2. 标题（### / ## / #）
        src = src.replace(/^###\s+(.+)$/gm, '<h4 class="md-h">$1</h4>');
        src = src.replace(/^##\s+(.+)$/gm, '<h3 class="md-h">$1</h3>');
        src = src.replace(/^#\s+(.+)$/gm, '<h2 class="md-h">$1</h2>');
        // 3. 加粗 / 斜体 / 行内代码
        src = src.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        src = src.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
        // 3.5 「参考资料」标题独立成块（避免与引用角标横排）
        src = src.replace(/^(参考资料|参考文献|参考|来源)([：:])?\s*$/gm, '<div class="md-refs-head">$1</div>');
        // 3.6 去掉正文行内的引用编号 [n]（仅保留"参考资料"块中行首的 [n] 文件名）
        // 提示词已要求正文不逐条标注，此处兜底清理旧回答 / LLM 未遵守的情况
        src = src.replace(/(?<!^)\[(\d+)\]/gm, '');
        // 4. 分割行（按换行拆成块，处理列表）
        const lines = src.split("\n");
        const out = [];
        let inUl = false, inOl = false;
        function closeLists() { if (inUl) { out.push('</ul>'); inUl = false; } if (inOl) { out.push('</ol>'); inOl = false; } }
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            const ulMatch = line.match(/^\s*[-*]\s+(.*)$/);
            const olMatch = line.match(/^\s*\d+[\.、]\s+(.*)$/);
            if (ulMatch) {
                if (inOl) closeLists();
                if (!inUl) { out.push('<ul class="md-ul">'); inUl = true; }
                out.push('<li>' + ulMatch[1] + '</li>');
            } else if (olMatch) {
                if (inUl) { out.push('</ul>'); inUl = false; }
                if (!inOl) { out.push('<ol class="md-ol">'); inOl = true; }
                out.push('<li>' + olMatch[1] + '</li>');
            } else {
                closeLists();
                if (line.trim() === "") { out.push('<div class="md-space"></div>'); }
                else { out.push('<p class="md-p">' + line + '</p>'); }
            }
        }
        closeLists();
        return out.join("");
    }

    function addMsg(role, text) {
        const m = document.createElement("div");
        m.className = "msg " + (role === "user" ? "msg-user" : "msg-ai");
        m.innerHTML = '<div class="msg-avatar">' + (role === "user" ? "🙋" : "🤖") + '</div><div class="msg-bubble"></div>';
        const bubble = m.querySelector(".msg-bubble");
        if (role === "user") bubble.textContent = text;
        else bubble.innerHTML = mdToHtml(text);
        chatBody.appendChild(m);
        const w = chatBody.querySelector(".welcome"); if (w) w.remove();
        scrollBottom(); return m;
    }
    function addTyping() {
        const m = document.createElement("div");
        m.className = "msg msg-ai typing";
        m.innerHTML = '<div class="msg-avatar">🤖</div><div class="msg-bubble"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>';
        chatBody.appendChild(m); scrollBottom(); return m;
    }
    function renderRefs(el, sources, container) {
        if (!sources || !sources.length) return;
        const wrap = document.createElement("div");
        wrap.className = "refs";
        wrap.innerHTML = '<div class="refs-title">参考资料（点击查看原文）</div>' + sources.slice(0, 8).map(function (s, i) {
            const name = String(s.source || "").split(/[\\/]/).pop();
            const docId = s.doc_id || "";
            const viewBtn = docId
                ? '<button class="ref-view" data-doc-id="' + esc(docId) + '" title="查看原文">查看原文</button>'
                : "";
            return '<div class="ref-item"><span class="ref-num">[' + (i + 1) + ']</span>' +
                '<span class="ref-file">' + esc(name) + '</span>' + viewBtn + '</div>';
        }).join("");
        (container || el.querySelector(".msg-bubble")).appendChild(wrap);
        wrap.querySelectorAll(".ref-view").forEach(function (b) {
            b.addEventListener("click", function () { viewDocument(b.dataset.docId); });
        });
        scrollBottom();
    }

    // v2.4 推荐追问卡片：点击直接作为新问题发送
    function renderFollowups(container, items) {
        if (!container || !items || !items.length) return;
        const wrap = document.createElement("div");
        wrap.className = "followups";
        wrap.innerHTML = '<div class="followups-title">猜你想问</div>' +
            '<div class="followup-chips">' +
            items.slice(0, 3).map(function (q) {
                return '<button type="button" class="followup-chip">' + esc(q) + '</button>';
            }).join("") + '</div>';
        container.appendChild(wrap);
        wrap.querySelectorAll(".followup-chip").forEach(function (chip) {
            chip.addEventListener("click", function () { sendQuestion(chip.textContent); });
        });
        scrollBottom();
    }

    /* ========== v1.5 回答评分组件（默认收起，hover 才展开） ========== */
    function renderFeedback(el, question, answer, sources, container) {
        const wrap = document.createElement("div");
        wrap.className = "feedback-bar";
        wrap.innerHTML =
            '<button class="fb-toggle" title="评价回答"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 00-3-3l-4 9v11h11.28a2 2 0 002-1.7l1.38-9a2 2 0 00-2-2.3zM7 22H4a2 2 0 01-2-2v-7a2 2 0 012-2h3"/></svg></button>' +
            '<div class="fb-expand">' +
                [1, 2, 3, 4, 5].map(function (n) {
                    return '<button class="feedback-star" data-score="' + n + '" title="' + n + '分">★</button>';
                }).join("") +
            '</div>';
        (container || el.querySelector(".msg-bubble")).appendChild(wrap);

        const toggleBtn = wrap.querySelector(".fb-toggle");
        const expand = wrap.querySelector(".fb-expand");

        toggleBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            var isOpen = expand.classList.toggle("fb-expand-open");
            toggleBtn.classList.toggle("fb-toggle-active", isOpen);
        });

        wrap.querySelectorAll(".feedback-star").forEach(function (btn) {
            btn.addEventListener("click", async function () {
                var score = parseInt(btn.dataset.score, 10);
                wrap.querySelectorAll(".feedback-star").forEach(function (s, i) {
                    s.classList.toggle("active", i < score);
                });
                toggleBtn.classList.add("fb-toggle-done");
                toggleBtn.title = "已评分 " + score + "/5";
                expand.classList.remove("fb-expand-open");
                toggleBtn.classList.remove("fb-toggle-active");
                try {
                    await json(API.feedback, {
                        method: "POST",
                        body: {
                            question: question,
                            answer: answer,
                            score: score,
                            sources: (sources || []).map(function (s) { return { source: s.source }; }),
                        },
                    });
                } catch (e) { /* 静默失败 */ }
            });
        });
        scrollBottom();
    }

    async function viewDocument(docId) {
        if (!docId) return;
        // 展示加载中弹窗
        openModal("查看原文",
            '<div class="empty-tip">加载中...</div>',
            function () { return true; });
        try {
            const data = await json(API.knowledgeDoc + docId);
            const name = String(data.source || "").split(/[\\/]/).pop();
            openModal("原文：" + name,
                '<div class="doc-view">' + esc(data.content || "(空)") + '</div>',
                function () { return true; });
        } catch (e) {
            openModal("查看原文",
                '<div class="result-msg err">加载失败：' + esc(e.message) + '</div>',
                function () { return true; });
        }
    }
    function renderAction(el, action) {
        if (!action) return;
        const c = document.createElement("div");
        c.className = "action-card";
        const isReal = action.mode === "http" && action.url && action.url.indexOf("/workflow/") !== 0;
        c.innerHTML = "✅ 已发起：<b>" + esc(action.workflow || "") + "</b>" +
            '<div class="ticket">工单：' + esc(action.ticket_id || "-") + '</div>';
        // 查看工单详情按钮：mock 模式弹窗展示，真实模式跳转
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "action-btn";
        if (isReal) {
            btn.textContent = "前往流程中心 →";
            btn.addEventListener("click", function () {
                window.open(action.url, "_blank");
            });
        } else {
            btn.textContent = "查看工单详情";
            btn.addEventListener("click", function () {
                const params = action.params || {};
                const paramsHtml = Object.keys(params).length
                    ? '<div class="doc-view" style="max-height:30vh;">' +
                      Object.keys(params).map(function (k) { return '<div>' + esc(k) + '：' + esc(JSON.stringify(params[k])) + '</div>'; }).join("") +
                      '</div>'
                    : '<div class="empty-tip">无额外参数</div>';
                openModal("工单详情",
                    '<div class="info-row"><span class="info-label">流程</span><span class="info-value">' + esc(action.workflow || "-") + '</span></div>' +
                    '<div class="info-row"><span class="info-label">工单号</span><span class="info-value" style="font-family:var(--font-mono);">' + esc(action.ticket_id || "-") + '</span></div>' +
                    '<div class="info-row"><span class="info-label">模式</span><span class="info-value">演示模式（未接入真实流程系统）</span></div>' +
                    '<div class="info-row"><span class="info-label">参数</span><span class="info-value">' + paramsHtml + '</span></div>',
                    function () { return true; });
            });
        }
        c.appendChild(btn);
        el.querySelector(".msg-bubble").appendChild(c); scrollBottom();
    }
    // 渲染可点击的流程列表：点击某个流程直接作为新问题发送
    function renderWorkflows(el, data) {
        const bubble = el.querySelector(".msg-bubble");
        const list = data.workflows || [];
        if (!list.length) {
            bubble.innerHTML = mdToHtml(data.answer || "暂无可用的流程");
            return;
        }
        // 直接用结构化数据渲染流程卡片，避免与 answer 文本重复
        bubble.innerHTML = '<div class="md-p">当前可用的流程如下：</div>';
        const wrap = document.createElement("div");
        wrap.className = "workflow-list";
        list.forEach(function (w) {
            const card = document.createElement("div");
            card.className = "workflow-item";
            const info = document.createElement("div");
            info.className = "workflow-info";
            info.innerHTML = '<div class="workflow-name">' + esc(w.name || "") + '</div>' +
                '<div class="workflow-desc">' + esc(w.description || "") + '</div>';
            const actions = document.createElement("div");
            actions.className = "workflow-actions";
            // 查看原文按钮
            const viewBtn = document.createElement("button");
            viewBtn.type = "button";
            viewBtn.className = "workflow-btn workflow-view";
            viewBtn.textContent = "查看原文";
            viewBtn.addEventListener("click", function () { viewWorkflowDoc(w.key); });
            // 发起按钮
            const launchBtn = document.createElement("button");
            launchBtn.type = "button";
            launchBtn.className = "workflow-btn workflow-launch";
            launchBtn.textContent = "发起申请";
            launchBtn.addEventListener("click", function () { sendQuestion("发起" + (w.name || "") + "申请"); });
            actions.appendChild(viewBtn);
            actions.appendChild(launchBtn);
            card.appendChild(info);
            card.appendChild(actions);
            wrap.appendChild(card);
        });
        bubble.appendChild(wrap);
        scrollBottom();
    }

    // v2.4 流程确认表单卡片：起草 → 用户补全/确认 → 定向提交 → 工单卡
    function renderDraft(el, draft) {
        const bubble = el.querySelector(".msg-bubble");
        const card = document.createElement("div");
        card.className = "draft-card";
        const missing = draft.missing || [];
        card.innerHTML =
            '<div class="draft-title">📋 ' + esc(draft.workflow_name || "流程申请") +
                '<span class="draft-badge">待确认</span></div>' +
            (missing.length
                ? '<div class="draft-missing">⚠ 还缺：' + esc(missing.join("、")) + '（表单中标 * 为必填）</div>'
                : '<div class="draft-missing" style="color:var(--accent-text);">✅ 信息已识别完整，确认后即可提交</div>') +
            '<div class="draft-grid">' +
                (draft.fields || []).map(function (f) {
                    const req = f.required ? '<span class="req">*</span>' : "";
                    const v = f.value == null ? "" : f.value;
                    let input;
                    if (f.type === "select") {
                        input = '<select data-key="' + esc(f.key) + '">' +
                            '<option value="">请选择</option>' +
                            (f.options || []).map(function (o) {
                                return '<option value="' + esc(o) + '"' + (o === v ? " selected" : "") + '>' + esc(o) + '</option>';
                            }).join("") + '</select>';
                    } else if (f.type === "textarea") {
                        input = '<textarea data-key="' + esc(f.key) + '" placeholder="选填">' + esc(v) + '</textarea>';
                    } else {
                        input = '<input type="' + esc(f.type === "number" ? "number" : (f.type === "date" ? "date" : "text")) +
                            '" data-key="' + esc(f.key) + '" value="' + esc(v) + '"' + (f.required ? ' placeholder="必填"' : '') + '>';
                    }
                    return '<div class="draft-field' + (f.type === "textarea" || f.type === "text" ? " full" : "") + '">' +
                        '<label>' + esc(f.label || f.key) + req + '</label>' + input + '</div>';
                }).join("") +
            '</div>' +
            '<div class="draft-actions">' +
                '<button type="button" class="btn-ghost draft-cancel">取消</button>' +
                '<button type="button" class="btn-primary draft-submit">确认提交</button>' +
            '</div>' +
            (draft.note ? '<div class="draft-note">依据你的描述：" ' + esc(String(draft.note).slice(0, 60)) + '"</div>' : "");
        bubble.appendChild(card);
        scrollBottom();

        function collect() {
            const params = {};
            card.querySelectorAll("[data-key]").forEach(function (input) {
                params[input.dataset.key] = input.value;
            });
            return params;
        }
        card.querySelector(".draft-cancel").addEventListener("click", function () {
            card.innerHTML = '<div class="draft-note">已取消本次申请。</div>';
        });
        card.querySelector(".draft-submit").addEventListener("click", async function () {
            const btn = card.querySelector(".draft-submit");
            const params = collect();
            const emptyRequired = (draft.fields || []).filter(function (f) {
                return f.required && !String(params[f.key] || "").trim();
            });
            if (emptyRequired.length) {
                alert("请先填写：" + emptyRequired.map(function (f) { return f.label; }).join("、"));
                return;
            }
            btn.disabled = true; btn.textContent = "提交中...";
            try {
                const data = await json(API.workflowSubmit, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ workflow_key: draft.workflow_key, params: params, session_id: SESSION_ID }),
                });
                card.innerHTML = "";
                renderAction(el, data.action);
                loadStats();
            } catch (e) {
                btn.disabled = false; btn.textContent = "确认提交";
                alert("提交失败：" + e.message);
            }
        });
    }

    async function viewWorkflowDoc(key) {
        if (!key) return;
        openModal("查看流程原文",
            '<div class="empty-tip">加载中...</div>',
            function () { return true; });
        try {
            const data = await json(API.workflowDoc + key + "/doc");
            if (!data.found || !data.answer) {
                openModal("查看流程原文",
                    '<div class="result-msg err">知识库中暂未检索到该流程的制度原文，请联系管理员补充文档。</div>',
                    function () { return true; });
                return;
            }
            openModal("流程原文：" + (data.workflow || key),
                '<div class="doc-view">' + esc(data.answer) + '</div>',
                function () { return true; });
        } catch (e) {
            openModal("查看流程原文",
                '<div class="result-msg err">加载失败：' + esc(e.message) + '</div>',
                function () { return true; });
        }
    }
    async function sendStream(typing, text) {
        typing.querySelector(".msg-bubble").innerHTML = "";
        const bubble = typing.querySelector(".msg-bubble");
        // v2.4 卡片式回答：头部（模型 + 状态 + 耗时）/ 正文 / 尾部（来源 + 评分）
        bubble.classList.add("msg-card");
        bubble.innerHTML =
            '<div class="answer-head">' +
                '<span class="answer-model">AI</span>' +
                '<span class="answer-status is-working">正在连接</span>' +
                '<span class="answer-elapsed"></span>' +
            '</div>' +
            '<div class="answer-body"></div>' +
            '<div class="answer-tail"></div>';
        const statusEl = bubble.querySelector(".answer-status");
        const elapsedEl = bubble.querySelector(".answer-elapsed");
        const bodyEl = bubble.querySelector(".answer-body");
        const tailEl = bubble.querySelector(".answer-tail");
        const t0 = Date.now();
        const timer = setInterval(function () {
            elapsedEl.textContent = ((Date.now() - t0) / 1000).toFixed(1) + "s";
        }, 100);

        let full = "";
        let streamSources = [];
        let streamError = "";
        let modelName = "";
        try {
            const cfg = await json(API.modelConfig).catch(function () { return null; });
            if (cfg && cfg.model) {
                modelName = cfg.model;
                bubble.querySelector(".answer-model").textContent = cfg.model;
            }
        } catch (e) { /* 忽略 */ }

        const res = await fetch(API.chatStream, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: text, session_id: SESSION_ID }),
        });
        if (!res.ok || !res.body) { clearInterval(timer); throw new Error("HTTP " + res.status); }
        const reader = res.body.getReader(), decoder = new TextDecoder("utf-8");
        let buffer = "";
        while (true) {
            const { value, done } = await reader.read(); if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf("\n\n")) !== -1) {
                const frame = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
                if (!frame.startsWith("data:")) continue;
                const payload = frame.slice(5).trim(); if (!payload) continue;
                let evt; try { evt = JSON.parse(payload); } catch (e) { continue; }
                if (evt.type === "status") {
                    statusEl.textContent = evt.message || "处理中";
                } else if (evt.type === "sources") {
                    streamSources = evt.sources || [];
                    renderRefs(typing, streamSources, tailEl);
                } else if (evt.type === "followups") {
                    renderFollowups(tailEl, evt.items);
                } else if (evt.type === "error") {
                    streamError = evt.message || "处理失败";
                    statusEl.classList.remove("is-working");
                    statusEl.textContent = "失败";
                    bodyEl.innerHTML =
                        '<div class="result-msg err" style="margin:10px 0 12px;">⚠ ' + esc(streamError) +
                        '<br><span style="font-size:12px;color:var(--text-3);">请到「模型设置」检查服务商与 API Key 是否匹配、额度是否充足</span></div>';
                } else if (evt.type === "token") {
                    if (statusEl.classList.contains("is-working")) {
                        statusEl.classList.remove("is-working");
                        statusEl.textContent = "";
                    }
                    full += evt.content || "";
                    bodyEl.innerHTML = mdToHtml(full);
                    scrollBottom();
                } else if (evt.type === "done") break;
            }
        }
        clearInterval(timer);
        statusEl.classList.remove("is-working");
        if (!streamError) statusEl.textContent = "";
        if (!full && !streamError) bodyEl.innerHTML = '<div class="md-p">抱歉，我暂时无法回答这个问题。</div>';
        renderFeedback(typing, text, full, streamSources, tailEl);
        if (chpOpen) loadHistorySessions();
    }
    async function sendQuestion(text) {
        if (!text || !text.trim()) return;
        addMsg("user", text.trim()); chatInput.value = "";
        const typing = addTyping(); sendBtn.disabled = true;
        try {
            const res = await fetch(API.agent, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ question: text.trim(), session_id: SESSION_ID }),
            });
            const data = await res.json();
            if (data.intent === "action") {
                if (data.draft) {
                    typing.querySelector(".msg-bubble").innerHTML = mdToHtml(data.answer || "");
                    renderDraft(typing, data.draft);
                } else {
                    typing.querySelector(".msg-bubble").innerHTML = mdToHtml(data.answer || "无法回答");
                    renderAction(typing, data.action);
                }
            } else if (data.intent === "list") {
                renderWorkflows(typing, data);
            } else { await sendStream(typing, text.trim()); }
        } catch (e) {
            typing.querySelector(".msg-bubble").textContent = "请求失败：" + e.message;
        } finally { sendBtn.disabled = false; chatInput.focus(); }
    }
    sendBtn.addEventListener("click", function () { sendQuestion(chatInput.value); });
    chatInput.addEventListener("keydown", function (e) { if (e.key === "Enter") sendQuestion(chatInput.value); });
    document.querySelectorAll(".chip").forEach(function (c) {
        c.addEventListener("click", function () { sendQuestion(c.dataset.q); });
    });
    $("clearBtn").addEventListener("click", async function () {
        await fetch(API.clear, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: SESSION_ID }),
        });
        location.reload();
    });
    function startNewChat() {
        SESSION_ID = newSessionId();
        chatBody.innerHTML = '<div class="welcome"><div class="welcome-icon"><svg width="48" height="48" viewBox="0 0 48 48" fill="none"><circle cx="24" cy="24" r="22" stroke="var(--accent)" stroke-width="1.5" opacity="0.4"/><path d="M29 20c0 4.14-3.36 7.5-7.5 7.5-1.05 0-2.05-.21-2.95-.6L15 27.5l.6-3.55C15.21 23.05 14.5 22.05 14.5 21c0-4.14 3.36-7.5 7.5-7.5S29 15.86 29 20z" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/></svg></div><h2>你好，我是企业知识助手</h2><p>我可以回答公司制度、SOP、技术文档相关问题，也可以为你发起请假、报销等流程。</p><div class="suggestions"><button class="chip chip-query" data-q="年假怎么申请？需要什么材料？">年假怎么申请？需要什么材料？</button><button class="chip chip-query" data-q="病假超过3天需要什么证明？">病假超过3天需要什么证明？</button><button class="chip chip-action" data-q="帮我申请3天事假">帮我申请3天事假</button><button class="chip chip-list" data-q="公司有哪些流程可以办理？">公司有哪些流程可以办理？</button></div></div>';
        chatBody.querySelectorAll(".chip").forEach(function (c) {
            c.addEventListener("click", function () { sendQuestion(c.dataset.q); });
        });
        chatInput.value = "";
        chatInput.focus();
    }
    $("newChatBtn").addEventListener("click", startNewChat);

    /* ========== 主题切换（深色/浅色） ========== */
    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        localStorage.setItem("theme", theme);
    }
    function initTheme() {
        const saved = localStorage.getItem("theme");
        // 默认深色；有保存则用保存值
        const theme = saved === "light" ? "light" : "dark";
        applyTheme(theme);
    }
    function toggleTheme() {
        const cur = document.documentElement.getAttribute("data-theme");
        applyTheme(cur === "light" ? "dark" : "light");
    }
    // 顶栏所有主题切换按钮（图标：深色显示太阳→切浅色，浅色显示月亮→切深色）
    document.querySelectorAll(".theme-toggle-btn").forEach(function (btn) {
        btn.addEventListener("click", toggleTheme);
    });

    /* ========== 初始化 ========== */
    initTheme();
    renderUserArea();
    loadStats();
    loadDashboard();
    setupQuickModelSwitcher();
    loadQuickModel();

    /* ========== v2.2: 对话历史管理 ========== */

    /* ========== 历史对话抽屉（合并自独立历史页，支持勾选批量删除） ========== */
    var chpOpen = false;

    function renderChpItems(sessions) {
        var list = $("chpSessionList");
        if (!sessions.length) {
            list.innerHTML = '<div class="empty-tip">暂无对话记录</div>';
            $("chpCount").textContent = "";
            return;
        }
        list.innerHTML = sessions.map(function(s) {
            return '<label class="chp-item' + (s.id === SESSION_ID ? " active" : "") + '" data-id="' + s.id + '">' +
                '<input type="checkbox" class="chp-check" data-id="' + s.id + '">' +
                '<div class="chp-info"><div class="chp-title">' + esc(s.title || "新对话") + '</div>' +
                '<div class="chp-meta"><span>' + s.message_count + ' 条</span><span>' + (s.updated_at || "").slice(5, 16) + '</span></div></div>' +
            '</label>';
        }).join("");
        $("chpCount").textContent = "共 " + sessions.length + " 个会话";
        list.querySelectorAll(".chp-item").forEach(function(el) {
            el.addEventListener("click", function(e) {
                if (e.target.classList.contains("chp-check")) return;
                openSessionChat(el.dataset.id);
            });
        });
    }

    async function loadHistorySessions() {
        if (!AUTH.isLoggedIn()) {
            $("chpSessionList").innerHTML = '<div class="empty-tip">登录后可查看对话历史</div>';
            $("chpCount").textContent = "";
            return;
        }
        try {
            var sessions = await json(API.sessions);
            renderChpItems(sessions || []);
        } catch(err) {
            $("chpSessionList").innerHTML = '<div class="empty-tip">加载失败：' + esc(err.message) + '</div>';
        }
    }

    function openSessionChat(sessionId) {
        // 把历史会话载入当前聊天窗口，继续提问会追加到同一会话
        SESSION_ID = sessionId;
        setSessionId(SESSION_ID);
        toggleHistoryPanel(false);
        loadSessionMessages(sessionId);
    }

    function toggleHistoryPanel(open) {
        chpOpen = (open === undefined) ? !chpOpen : open;
        $("chatHistoryPanel").classList.toggle("open", chpOpen);
        if (chpOpen) {
            $("chpSearchInput").value = "";
            loadHistorySessions();
        }
    }

    async function deleteSelectedSessions() {
        var ids = Array.from(document.querySelectorAll("#chpSessionList .chp-check:checked")).map(function(c) { return c.dataset.id; });
        if (!ids.length) { alert("请先勾选要删除的会话"); return; }
        if (!confirm("确认删除选中的 " + ids.length + " 个会话？删除后不可恢复。")) return;
        var failed = 0;
        for (var i = 0; i < ids.length; i++) {
            try { await json(API.sessions + "/" + ids[i], { method: "DELETE" }); }
            catch (e) { failed++; }
        }
        if (failed) alert("有 " + failed + " 个会话删除失败");
        if (ids.indexOf(SESSION_ID) !== -1) startNewChat();
        loadHistorySessions();
    }

    async function searchHistory(keyword) {
        if (!keyword.trim()) { loadHistorySessions(); return; }
        try {
            var results = await json(API.historySearch + "?q=" + encodeURIComponent(keyword));
            if (!results.length) {
                $("chpSessionList").innerHTML = '<div class="empty-tip">未找到匹配的对话</div>';
                $("chpCount").textContent = "";
                return;
            }
            var seen = {}, items = [];
            results.forEach(function(m) {
                if (!seen[m.session_id]) { seen[m.session_id] = 1; items.push(m); }
            });
            $("chpSessionList").innerHTML = items.map(function(m) {
                return '<label class="chp-item" data-id="' + m.session_id + '">' +
                    '<input type="checkbox" class="chp-check" data-id="' + m.session_id + '">' +
                    '<div class="chp-info"><div class="chp-title">' + esc(m.session_title || "新对话") + '</div>' +
                    '<div class="chp-meta"><span>' + esc(m.content.slice(0, 30)) + '...</span></div></div></label>';
            }).join("");
            $("chpCount").textContent = "搜索到 " + items.length + " 个会话";
            $("chpSessionList").querySelectorAll(".chp-item").forEach(function(el) {
                el.addEventListener("click", function(e) {
                    if (e.target.classList.contains("chp-check")) return;
                    openSessionChat(el.dataset.id);
                });
            });
        } catch(err) {
            $("chpSessionList").innerHTML = '<div class="empty-tip">搜索失败</div>';
        }
    }

    async function loadSessionMessages(sessionId) {
        try {
            var messages = await json(API.sessionMessages + sessionId + "/messages");
            chatBody.innerHTML = "";
            messages.forEach(function(m) {
                if (m.role === "user") {
                    addMsg("user", m.content);
                } else if (m.role === "ai" || m.role === "assistant") {
                    // 兼容两种角色名：后端保存为 "assistant"，历史展示用 "ai"
                    addMsg("ai", m.content);
                }
            });
            if (!messages.length) {
                chatBody.innerHTML = '<div class="welcome"><div class="welcome-icon"><svg width="48" height="48" viewBox="0 0 48 48" fill="none"><circle cx="24" cy="24" r="22" stroke="var(--accent)" stroke-width="1.5" opacity="0.4"/><path d="M29 20c0 4.14-3.36 7.5-7.5 7.5-1.05 0-2.05-.21-2.95-.6L15 27.5l.6-3.55C15.21 23.05 14.5 22.05 14.5 21c0-4.14 3.36-7.5 7.5-7.5S29 15.86 29 20z" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/></svg></div><h2>继续对话</h2><p>下面是该会话的历史消息，你可以继续提问。</p></div>';
            }
            chatInput.focus();
            scrollBottom();
        } catch(err) {
            showError("加载历史消息失败：" + err.message);
        }
    }

    /* ========== v2.3: 文档清洗上传 ========== */

    function currentCleanKb() {
        const sel = $("cleanKbSelect");
        return sel ? (sel.value || "default") : "default";
    }

    async function loadCleanKbSelect() {
        const sel = $("cleanKbSelect");
        if (!sel) return;
        try {
            const data = await json(API.kbList);
            const kbs = data.knowledge_bases || [];
            const cur = sel.value || "default";
            sel.innerHTML = kbs.map(function (kb) {
                return '<option value="' + esc(kb.id) + '">' + esc(kb.name) + '</option>';
            }).join("");
            if (Array.from(sel.options).some(function (o) { return o.value === cur; })) sel.value = cur;
        } catch (e) { /* 保留默认 */ }
        sel.addEventListener("change", function () { loadCleanDocs(true); });
    }

    async function uploadCleanFiles(files) {
        const r = $("cleanUploadResult");
        if (r) { r.className = "result-msg"; r.textContent = "清洗上传中..."; }
        const kb = currentCleanKb();
        const results = [];
        for (const file of files) {
            const form = new FormData(); form.append("file", file); form.append("kb_id", kb);
            try {
                const data = await json(API.upload, { method: "POST", body: form });
                if (data.skipped) {
                    results.push("⏭ " + file.name + " · " + (data.message || ""));
                } else if (data.overwritten) {
                    results.push("♻ " + file.name + " · 覆盖更新 " + (data.chunks || 0) + " 片段");
                } else {
                    results.push("✅ " + file.name + " · 清洗入库 " + (data.chunks || 0) + " 片段");
                }
            } catch (e) { results.push("✕ " + file.name + " 失败：" + (e.message || "")); }
        }
        if (r) {
            r.classList.add(results.every(function (x) { return x.indexOf("失败") < 0; }) ? "ok" : "err");
            r.textContent = results.join("\n");
        }
        loadCleanDocs(true);
    }

    let _cleanDocsCache = null, _cleanDocsCacheTime = 0;
    const CLEAN_DOCS_CACHE_TTL = 5000;
    async function loadCleanDocs(force) {
        const wrap = $("cleanDocList");
        if (!wrap) return;
        const now = Date.now();
        if (!force && _cleanDocsCache && (now - _cleanDocsCacheTime) < CLEAN_DOCS_CACHE_TTL) {
            renderCleanDocs(_cleanDocsCache, wrap);
            return;
        }
        wrap.innerHTML = '<div class="empty-tip">加载中...</div>';
        try {
            const data = await json(API.list + "?kb_id=" + encodeURIComponent(currentCleanKb()));
            _cleanDocsCache = data;
            _cleanDocsCacheTime = now;
            renderCleanDocs(data, wrap);
        } catch (e) {
            wrap.innerHTML = '<div class="empty-tip">加载失败：' + esc(e.message) + '</div>';
        }
    }

    function renderCleanDocs(data, wrap) {
        const docs = (data.documents || []).slice(0, 50);
        const total = data.total || docs.length;
        if (!docs.length) { wrap.innerHTML = '<div class="empty-tip">暂无文档</div>'; return; }
        const isAdmin = AUTH.getUser() && AUTH.getUser().role === "admin";
        const kb = currentCleanKb();
        const header = total > docs.length
            ? '<div class="empty-tip" style="padding:6px;">共 ' + total + ' 份文档，显示前 ' + docs.length + ' 份</div>'
            : "";
        function fmtTime(iso) {
            if (!iso) return "";
            var d = new Date(iso);
            if (isNaN(d.getTime())) return "";
            var now = new Date();
            var sameYear = d.getFullYear() === now.getFullYear();
            var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
            if (sameYear) {
                return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + pad(d.getHours()) + ":" + pad(d.getMinutes());
            }
            return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " + pad(d.getHours()) + ":" + pad(d.getMinutes());
        }
        wrap.innerHTML = header + docs.map(function (d) {
            const name = String(d.source || "").split(/[\\/]/).pop();
            const delBtn = isAdmin ? '<button class="doc-del" data-id="' + esc(d.doc_id) + '">✕</button>' : "";
            const t = d.updated_at || d.created_at;
            const time = fmtTime(t);
            return '<div class="doc-item"><div class="doc-info"><div class="doc-name">📄 ' + esc(name) +
                '</div><div class="doc-meta">' + (d.chunks || 0) + ' 个片段' + (time ? " · " + esc(time) : "") + '</div></div>' +
                delBtn + '</div>';
        }).join("");
        wrap.querySelectorAll(".doc-del").forEach(function (b) {
            b.addEventListener("click", async function () {
                if (!confirm("确认删除？")) return;
                try {
                    await json(API.list + "/" + b.dataset.id + "?kb_id=" + encodeURIComponent(kb), { method: "DELETE" });
                    loadCleanDocs(true);
                }
                catch (e) { alert("删除失败：" + e.message); }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function() {
        // 历史对话抽屉（合并自独立历史页）
        var hp = $("historyPanelBtn"); if (hp) hp.addEventListener("click", function() { toggleHistoryPanel(); });
        var hc = $("chpCloseBtn"); if (hc) hc.addEventListener("click", function() { toggleHistoryPanel(false); });
        var hsi = $("chpSearchInput"); if (hsi) hsi.addEventListener("input", function() {
            clearTimeout(window._historySearchTimer);
            window._historySearchTimer = setTimeout(function() { searchHistory(hsi.value); }, 500);
        });
        var hdel = $("chpDeleteBtn"); if (hdel) hdel.addEventListener("click", deleteSelectedSessions);
        var hsa = $("chpSelectAll"); if (hsa) hsa.addEventListener("change", function() {
            document.querySelectorAll("#chpSessionList .chp-check").forEach(function(c) { c.checked = hsa.checked; });
        });

        // 文档清洗上传视图初始化
        var mr = $("memoryRefreshBtn"); if (mr) mr.addEventListener("click", function () { loadCleanDocs(true); });
        var cz = $("cleanUploadZone"), cf = $("cleanFileInput");
        if (cz && cf) {
            cz.addEventListener("click", function () { cf.click(); });
            cf.addEventListener("change", function () { if (cf.files.length) uploadCleanFiles(cf.files); });
            ["dragover", "dragenter"].forEach(function (ev) {
                cz.addEventListener(ev, function (e) { e.preventDefault(); cz.classList.add("dragover"); });
            });
            ["dragleave", "drop"].forEach(function (ev) {
                cz.addEventListener(ev, function (e) { e.preventDefault(); cz.classList.remove("dragover"); });
            });
            cz.addEventListener("drop", function (e) {
                if (e.dataTransfer.files.length) uploadCleanFiles(e.dataTransfer.files);
            });
        }
    });

    var origSwitchPage = switchPage;
    switchPage = function(view) {
        origSwitchPage(view);
        if (view === "history") loadHistorySessions();
        if (view === "memory") { loadCleanKbSelect(); loadCleanDocs(true); }
    };

})();
