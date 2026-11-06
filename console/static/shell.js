// The console's one navigation, on every page: six items (Home, Infrastructure, Serverless,
// JFrog OIDC, Runs, Settings), each a list page that unfolds into its sub-items, as in Azure DevOps: a sub-item is
// that list filtered (a service type, a cloud, a run area). A classic script placed inside <aside class="sidenav">: it
// writes the menu where it stands, before the page's own module runs, so the first paint already has it.
// Also window.consoleBlade: the list pages' filter bar ("Filter for any field…" and filter pills).
(() => {
  // Official service icons and line icons, inlined: no CDN.
  const ICON = {"home": "<svg class=\"ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M3 10.5 12 3l9 7.5\"/><path d=\"M5 9.5V20h5v-6h4v6h5V9.5\"/></svg>", "infra": "<svg class=\"ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect x=\"3\" y=\"4\" width=\"18\" height=\"7\" rx=\"1.5\"/><rect x=\"3\" y=\"13\" width=\"18\" height=\"7\" rx=\"1.5\"/><path d=\"M7 7.5h.01M7 16.5h.01\"/></svg>", "runs": "<svg class=\"ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M5 6h14M5 12h14M5 18h9\"/><path d=\"M17 15.5l4 2.5-4 2.5z\"/></svg>", "run": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><g><g><polygon points=\"8.9 2.63 12.02 12 21.38 12 8.9 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /><polygon points=\"21.38 12 12.02 12 8.9 21.38 21.38 12\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 21.38 6.57 19.81 8.9 12 5.78 12 3.44 21.38\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 2.63 5.78 12 8.9 12 6.57 4.19 3.44 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /></g></g></svg>", "aca": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><g><g><g><g><path d=\"M16.932,11.578a8.448,8.448,0,0,1-7.95,5.59,8.15,8.15,0,0,1-2.33-.33,2.133,2.133,0,0,0,.18-.83c.01,0,.03.01.04.01a7.422,7.422,0,0,0,2.11.3,7.646,7.646,0,0,0,6.85-4.28l.01-.01Z\" fill=\"#32bedd\" /><path d=\"M3.582,14.068a2.025,2.025,0,0,0-.64.56,8.6,8.6,0,0,1-1.67-2.44l1.04.23v.26a.6.6,0,0,0,.47.59l.14.03a6.136,6.136,0,0,0,.62.73Z\" fill=\"#32bedd\" /><path d=\"M12.352.958a2.28,2.28,0,0,0-.27.81c-.02-.01-.05-.02-.07-.03a7.479,7.479,0,0,0-3.03-.63,7.643,7.643,0,0,0-5.9,2.8l-.29.06a.6.6,0,0,0-.48.58v.46l-1.02.19A8.454,8.454,0,0,1,8.982.268,8.6,8.6,0,0,1,12.352.958Z\" fill=\"#32bedd\" /><path d=\"M16.872,5.7l-1.09-.38a6.6,6.6,0,0,0-.72-1.16c-.02-.03-.04-.05-.05-.07a2.083,2.083,0,0,0,.72-.45A7.81,7.81,0,0,1,16.872,5.7Z\" fill=\"#32bedd\" /><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"#fff\" /><g><g><path d=\"M1.1,5.668l1.21-.23v6.55l-1.23-.27-.99-.22a.111.111,0,0,1-.09-.12v-5.4a.12.12,0,0,1,.09-.12Z\" fill=\"#a67af4\" /></g><g><g><g><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"url(#sl-aca-g0)\" /><path d=\"M8.586,3.3,2.878,4.378a.177.177,0,0,0-.14.175V12.68a.177.177,0,0,0,.137.174L8.581,14.1a.176.176,0,0,0,.21-.174V3.478A.175.175,0,0,0,8.619,3.3Z\" fill=\"url(#sl-aca-g1)\" /></g></g><polygon points=\"5.948 4.921 5.948 12.483 7.934 12.814 7.934 4.564 5.948 4.921\" fill=\"#b796f9\" opacity=\"0.5\" /><polygon points=\"3.509 5.329 3.509 11.954 5.238 12.317 5.238 5.031 3.509 5.329\" fill=\"#b796f9\" opacity=\"0.5\" /></g></g></g><path d=\"M16,2.048a1.755,1.755,0,1,1-1.76-1.76A1.756,1.756,0,0,1,16,2.048Z\" fill=\"#32bedd\" /><circle cx=\"4.65\" cy=\"15.973\" r=\"1.759\" fill=\"#32bedd\" /></g><path d=\"M18,6.689v3.844a.222.222,0,0,1-.133.2l-.766.316-3.07,1.268-.011,0a.126.126,0,0,1-.038,0,.1.1,0,0,1-.1-.1V5.234a.1.1,0,0,1,.054-.088l0,0,.019,0a.031.031,0,0,1,.019,0,.055.055,0,0,1,.034.008l.011,0,.012,0L17.05,6.2l.8.282A.213.213,0,0,1,18,6.689Z\" fill=\"#773adc\" /><path d=\"M13.959,5.14l-3.8.715a.118.118,0,0,0-.093.117v5.409a.118.118,0,0,0,.091.116l3.8.831a.115.115,0,0,0,.137-.09.109.109,0,0,0,0-.026V5.256a.117.117,0,0,0-.115-.118A.082.082,0,0,0,13.959,5.14Z\" fill=\"#a67af4\" /></g></g></svg>", "func": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><g><path d=\"M2.37,7.475H3.2a.267.267,0,0,1,.267.267v6.148a.533.533,0,0,1-.533.533H2.1a0,0,0,0,1,0,0V7.741a.267.267,0,0,1,.267-.267Z\" transform=\"translate(12.507 16.705) rotate(134.919)\" fill=\"#50e6ff\" /><path d=\"M2.325,3.6h.833a.267.267,0,0,1,.267.267v6.583a0,0,0,0,1,0,0H2.591a.533.533,0,0,1-.533-.533V3.865A.267.267,0,0,1,2.325,3.6Z\" transform=\"translate(5.759 0.114) rotate(44.919)\" fill=\"#1490df\" /></g><g><path d=\"M14.53,7.475h.833a.533.533,0,0,1,.533.533v6.148a.267.267,0,0,1-.267.267H14.8a.267.267,0,0,1-.267-.267V7.475a0,0,0,0,1,0,0Z\" transform=\"translate(12.223 -7.555) rotate(45.081)\" fill=\"#50e6ff\" /><path d=\"M15.108,3.6h.833a0,0,0,0,1,0,0v6.583a.267.267,0,0,1-.267.267h-.833a.267.267,0,0,1-.267-.267V4.131a.533.533,0,0,1,.533-.533Z\" transform=\"translate(31.022 1.222) rotate(135.081)\" fill=\"#1490df\" /></g><path d=\"M8.459,9.9H4.87a.193.193,0,0,1-.2-.181.166.166,0,0,1,.018-.075L8.991,1.13a.206.206,0,0,1,.186-.106h4.245a.193.193,0,0,1,.2.181.165.165,0,0,1-.035.1L8.534,7.966h4.928a.193.193,0,0,1,.2.181.176.176,0,0,1-.052.122L5.421,16.788c-.077.046-.624.5-.356-.189h0Z\" fill=\"url(#sl-func-g0)\" /></svg>", "fn": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><g><g><g><polygon points=\"2 15.56 5 18 5 6 2 8.43 2 15.56\" fill=\"#669df6\" /><polygon points=\"2 10 5 8 5 6 2 10\" fill=\"#4285f4\" /><polygon points=\"5 18 5 16 2 14 5 18\" fill=\"#4285f4\" /><polygon points=\"8 20 2 14 2 18 6 22 8 20\" fill=\"#aecbfa\" /></g></g><polygon points=\"5.99 2.01 2 6 2.01 10 7.99 4.01 5.99 2.01\" fill=\"#aecbfa\" /><g><g><polygon points=\"22 8.45 19 6.01 19 18.01 22 15.59 22 8.45\" fill=\"#669df6\" /><polygon points=\"22 14.01 19 16.01 19 18.01 22 14.01\" fill=\"#4285f4\" /><polygon points=\"19 6.01 19 8.01 22 10.01 19 6.01\" fill=\"#4285f4\" /><polygon points=\"16 4.01 22 10.01 22 6.01 18 2.01 16 4.01\" fill=\"#aecbfa\" /></g></g><polygon points=\"18.01 22 22 18.01 21.99 14.01 16.01 20 18.01 22\" fill=\"#aecbfa\" /><circle cx=\"8\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /><circle cx=\"12\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /><circle cx=\"15.99\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /></g></svg>"};
  const DEFS = "<svg class=\"mark-defs\" width=\"0\" height=\"0\" aria-hidden=\"true\" focusable=\"false\"><defs><linearGradient id=\"sl-aca-g0\" x1=\"8.798\" y1=\"8.703\" x2=\"14.683\" y2=\"8.703\" gradientUnits=\"userSpaceOnUse\"><stop offset=\"0.001\" stop-color=\"#773adc\" /><stop offset=\"1\" stop-color=\"#552f99\" /></linearGradient><linearGradient id=\"sl-aca-g1\" x1=\"5.764\" y1=\"3.777\" x2=\"5.764\" y2=\"13.78\" gradientUnits=\"userSpaceOnUse\"><stop offset=\"0\" stop-color=\"#a67af4\" /><stop offset=\"0.999\" stop-color=\"#773adc\" /></linearGradient><linearGradient id=\"sl-func-g0\" x1=\"-175.993\" y1=\"-343.723\" x2=\"-175.993\" y2=\"-359.232\" gradientTransform=\"translate(212.573 370.548) scale(1.156 1.029)\" gradientUnits=\"userSpaceOnUse\"><stop offset=\"0\" stop-color=\"#fea11b\" /><stop offset=\"0.284\" stop-color=\"#fea51a\" /><stop offset=\"0.547\" stop-color=\"#feb018\" /><stop offset=\"0.8\" stop-color=\"#ffc314\" /><stop offset=\"1\" stop-color=\"#ffd70f\" /></linearGradient></defs></svg>";
  // Each cloud's real logo: constant inline SVG, no user data.
  const CLOUD_LOGO = {
    gcp: '<svg class="mark cloud-logo" viewBox="0 0 24 19.3" aria-hidden="true"><path fill="#EA4335" d="M15.2 5.3h.7l2-2 .1-.9C14.2-.9 8.5-.5 5.2 3.3c-.9 1-1.6 2.3-2 3.6.2-.1.5-.1.7 0l4-.7s.2-.3.3-.3c1.8-2 4.8-2.2 6.9-.5h.1z"/><path fill="#4285F4" d="M20.8 6.9c-.5-1.7-1.4-3.2-2.7-4.4l-2.8 2.8c1.2 1 1.9 2.5 1.8 4v.5c1.4 0 2.5 1.1 2.5 2.5s-1.1 2.5-2.5 2.5h-5l-.5.5v3l.5.5h5c3.6 0 6.5-2.9 6.5-6.5 0-2.2-1.1-4.2-2.8-5.4z"/><path fill="#34A853" d="M7.1 19.3h5v-4h-5c-.4 0-.7-.1-1-.2l-.7.2-2 2-.2.7c1.1.8 2.5 1.3 3.9 1.3z"/><path fill="#FBBC05" d="M7.1 6.3C3.5 6.3.6 9.3.6 12.9c0 2 1 3.9 2.6 5.2l2.9-2.9c-1.3-.6-1.8-2.1-1.3-3.3.6-1.3 2.1-1.8 3.3-1.3.6.3 1 .7 1.3 1.3l2.9-2.9c-1.2-1.6-3.1-2.6-5.2-2.6z"/></svg>',
    azure: '<svg class="mark cloud-logo" viewBox="0 0 24 24" aria-hidden="true"><path fill="#0078D4" d="M5.483 21.3H24L14.025 4.013l-3.038 8.347 5.836 6.938L5.483 21.3zM13.23 2.7L6.105 8.677 0 19.253h5.505v.014L13.23 2.7z"/></svg>',
  };
  ICON.web = "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><circle cx=\"9\" cy=\"9\" r=\"8\" fill=\"#0078d4\"/><path d=\"M9 1.2a7.8 7.8 0 0 1 6.9 4.2H2.1A7.8 7.8 0 0 1 9 1.2z\" fill=\"#5ea0ef\"/><g fill=\"none\" stroke=\"#fff\" stroke-width=\"1\" stroke-linecap=\"round\"><path d=\"M9 1.4c-2.4 2.2-2.4 13 0 15.2M9 1.4c2.4 2.2 2.4 13 0 15.2\"/><path d=\"M1.4 9h15.2M2.4 5.4h13.2M2.4 12.6h13.2\"/></g></svg>";   // App Service
  ICON.boot = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4.5 6v5.5c0 4.4 3.1 8.2 7.5 9.5 4.4-1.3 7.5-5.1 7.5-9.5V6z"/><circle cx="12" cy="10.5" r="2.2"/><path d="M12 12.7V16.5M12 15h1.8"/></svg>';
  ICON.serverless = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2.5 5 13.5h6l-1 8 8-11h-6z"/></svg>';
  ICON.settings = '<svg class="mark nav-ico" viewBox="0 0 24 24" aria-hidden="true"><path fill="#605E5C" d="M13.7 2l.5 2.6c.6.2 1.2.5 1.7.9l2.5-.9 1.7 3-2 1.7c.1.6.1 1.2 0 1.8l2 1.7-1.7 3-2.5-.9c-.5.4-1.1.7-1.7.9l-.5 2.6h-3.4l-.5-2.6c-.6-.2-1.2-.5-1.7-.9l-2.5.9-1.7-3 2-1.7a5 5 0 0 1 0-1.8l-2-1.7 1.7-3 2.5.9c.5-.4 1.1-.7 1.7-.9L10.3 2z"/><circle cx="12" cy="10.5" r="3" fill="#fff"/></svg>';
  const BOOT = ICON.boot;
  // The sidebar's top items in their original colours (Azure portal style), not line drawings.
  const NAV_ICON = {"home": "<svg class=\"mark nav-ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#0078D4\" d=\"M12 2.2 1.6 11.1h3V21h5.6v-5.6h3.6V21h5.6v-9.9h3z\"/><path fill=\"#50E6FF\" d=\"M12 2.2 1.6 11.1h3L12 4.8l7.4 6.3h3z\"/></svg>", "infra": "<svg class=\"mark nav-ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect x=\"2.5\" y=\"3.5\" width=\"19\" height=\"7.5\" rx=\"1.6\" fill=\"#0078D4\"/><rect x=\"2.5\" y=\"13\" width=\"19\" height=\"7.5\" rx=\"1.6\" fill=\"#50A0E4\"/><circle cx=\"6.5\" cy=\"7.25\" r=\"1.2\" fill=\"#fff\"/><circle cx=\"6.5\" cy=\"16.75\" r=\"1.2\" fill=\"#fff\"/><path d=\"M10 7.25h8M10 16.75h8\" stroke=\"#fff\" stroke-width=\"1.4\" stroke-linecap=\"round\"/></svg>", "serverless": "<svg class=\"mark nav-ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#FFB900\" d=\"M13.6 1.8 4.4 13.6h6.2l-1.4 8.6 9.6-12.4h-6.4z\"/><path fill=\"#F7D046\" d=\"M13.6 1.8 4.4 13.6h3.4L13.6 5z\"/></svg>", "oidc": "<svg class=\"mark nav-ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#40BE46\" d=\"M12 1.5 21.1 6.75v10.5L12 22.5l-9.1-5.25V6.75z\"/><path fill=\"#fff\" d=\"M12 7.2l4.2 2.4v4.8L12 16.8l-4.2-2.4V9.6z\"/></svg>", "runs": "<svg class=\"mark nav-ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#0078D7\" d=\"M0 8.877 2.247 5.91l8.405-3.416V.022l7.37 5.393L2.966 8.338v8.225L0 15.707zm24-4.45v14.651l-5.753 4.9-9.303-3.057v3.056l-5.978-7.416 15.057 1.798V5.415z\"/></svg>"};

  // Every service the lists show: key → {key, label, cloud, cloudId, group, icon}. Azure first, then Google Cloud.
  const KINDS = [
    ["azure", "Azure", "Serverless", [["azure_container_app_job", "Container Apps job", "aca"], ["azure_container_app", "Container App", "aca"],
      ["azure_functions_aca", "Functions on Container Apps", "func"], ["azure_functions_premium", "Functions Premium", "func"],
      ["azure_functions_flex", "Functions Flex Consumption", "func"]]],
    ["azure", "Azure", "App Services", [["azure_web_app", "Web App", "web"]]],
    ["gcp", "Google Cloud", "Serverless", [["gcp_run_job", "Cloud Run job", "run"], ["gcp_run_service", "Cloud Run service", "run"],
      ["gcp_run_function", "Cloud Run function", "fn"], ["gcp_run_worker_pool", "Cloud Run worker pool", "run"]]],
    ["gcp", "Google Cloud", "App Services", [["gcp_app_engine", "App Engine", "web"]]],
  ];
  const SECTIONS = {};
  for (const [cloudId, cloud, group, items] of KINDS) {
    for (const [key, label, icon] of items) SECTIONS[key] = { key, label, cloud, cloudId, group, icon };
  }
  const sectionOfKind = (kind) => (SECTIONS[kind] ? kind : null);

  // Seven top items, as in Azure DevOps: each opens its own sub-items (a filtered view of its list).
  // [key, label, href, icon, [[label, href, icon, when(path, query) → current?]]]
  const q = (k, v) => (p, u) => u.get(k) === v;
  const SHORT = { azure_container_app_job: "Container Apps jobs", azure_container_app: "Container Apps",
    azure_functions_aca: "Functions on ACA", azure_functions_premium: "Functions Premium", azure_functions_flex: "Functions Flex", azure_web_app: "Web Apps",
    gcp_run_job: "Cloud Run jobs", gcp_run_service: "Cloud Run services", gcp_run_function: "Cloud Run functions",
    gcp_run_worker_pool: "Worker pools", gcp_app_engine: "App Engine" };
  // Serverless and App Services each open by cloud, never mixed: Azure and its services, then Google Cloud.
  // Each lists from its own mount of the workload pages: /serverless/<cloud>, /app-services/<cloud>.
  const GROUP = (group, base) => KINDS.filter(([, , g]) => g === group).flatMap(([c, cloud, , items]) => [
    [cloud, `${base}/${c}`, c, (p, u) => !u.get("type") && (p.startsWith(`${base}/${c}`) || (p.startsWith(base) && u.get("cloud") === c)), 1],
    ...items.map(([k, , ic]) => [SHORT[k], `${base}/${c}?type=${k}`, ic, (p, u) => u.get("type") === k || sel.kind === k, 2])]);
  const SERVERLESS = GROUP("Serverless", "/serverless");
  const APP_SERVICES = GROUP("App Services", "/app-services");
  const BY_CLOUD = (base, path) => [["Azure", `${base}${path("azure")}`, "azure", (p, u) => u.get("cloud") === "azure" || p.endsWith("-azure")],
    ["Google Cloud", `${base}${path("gcp")}`, "gcp", (p, u) => u.get("cloud") === "gcp" || p.endsWith("-gcp")]];
  const NAV = [["home", "Home", "/", "home", []],
    ["infra", "Infrastructure", "/setup", "infra", BY_CLOUD("/setup", (c) => `?cloud=${c}`)],
    ["serverless", "Serverless", "/serverless/", "serverless", SERVERLESS],
    ["apps", "App Services", "/app-services/", "web", APP_SERVICES],
    ["oidc", "JFrog OIDC", "/oidc", "boot", BY_CLOUD("/oidc", (c) => `?cloud=${c}`)],
    ["runs", "Runs", "/runs", "runs", [["Serverless", "/runs?area=serverless", "serverless", q("area", "serverless")],
      ["App Services", "/runs?area=app-services", "web", q("area", "app-services")],
      ["Infrastructure", "/runs?area=infrastructure", "infra", q("area", "infrastructure")],
      ["JFrog OIDC", "/runs?area=oidc", "boot", q("area", "oidc")]]],
    ["settings", "Settings", "/settings", "settings", []]];

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const glyph = (k) => (k === "boot" ? BOOT : CLOUD_LOGO[k] || NAV_ICON[k] || ICON[k] || "");
  const CHEV = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>';
  // A fresh open or a refresh starts fully collapsed (the current section stays tinted); the chevrons
  // open it. Folds made in the tab (sections and cloud groups) carry over when a sidebar link opens the
  // next page, so the item clicked stays in view; a refresh clears them. A new tab has none.
  const FOLD_KEY = "console-nav-folds";
  const isReload = performance.getEntriesByType?.("navigation")[0]?.type === "reload";
  const folds = (() => {
    if (isReload) { try { sessionStorage.removeItem(FOLD_KEY); } catch { /* memory only */ } return {}; }
    try { return JSON.parse(sessionStorage.getItem(FOLD_KEY)) || {}; } catch { return {}; }
  })();
  const opened = new Set(folds.blocks || []);
  const openSubs = new Set(folds.subs || []);
  const saveFolds = () => {
    try { sessionStorage.setItem(FOLD_KEY, JSON.stringify({ blocks: [...opened], subs: [...openSubs] })); } catch { /* memory only */ }
  };
  const block = ([key, label, href, icon, kids]) => {
    const top = `<a class="nav-item" href="${esc(href)}" data-section data-nav="${key}">${NAV_ICON[key] || glyph(icon)}<span>${esc(label)}</span></a>`;
    if (!kids.length) return `<div class="nav-block" data-block="${key}">${top}</div>`;
    const item = ([l, h, ic, , lvl], i) =>
      `<a class="nav-item nav-child${lvl === 1 ? " nav-cloud-row" : lvl === 2 ? " nav-grand" : ""}" href="${esc(h)}" data-child="${key}:${i}">${glyph(ic)}<span>${esc(l)}</span></a>`;
    // A cloud row with services under it folds on its own chevron, like the section above it.
    let sub = "";
    for (let i = 0; i < kids.length; i++) {
      if (kids[i][4] !== 1 || kids[i + 1]?.[4] !== 2) { sub += item(kids[i], i); continue; }
      const id = `${key}:${kids[i][2]}`, at = i;
      let grand = "";
      while (kids[i + 1]?.[4] === 2) { i++; grand += item(kids[i], i); }
      sub += `<div class="cloudfold" data-sub="${esc(id)}">${item(kids[at], at)}<button type="button" class="cloudfold-chev" aria-label="Show ${esc(kids[at][0])} items" aria-expanded="true">${CHEV}</button>` +
        `<div class="cloudfold-kids">${grand}</div></div>`;
    }
    return `<div class="nav-block" data-block="${key}">${top}<button type="button" class="nav-chev" aria-label="Show ${esc(label)} items" aria-expanded="false" aria-controls="nav-kids-${key}">${CHEV}</button>` +
      `<div class="nav-kids" id="nav-kids-${key}"><div class="nav-kids-in">${sub}</div></div></div>`;
  };
  function fromPath() {
    const p = location.pathname;
    if (p === "/") return "home";
    if (/^\/oidc(\/|$)/.test(p)) return "oidc";
    if (/^\/setup(\/|$)/.test(p)) return "infra";
    if (/^\/runs(\/|$)/.test(p)) return "runs";
    if (/^\/settings(\/|$)/.test(p)) return "settings";
    if (/^\/app-services(\/|$)/.test(p)) return "apps";
    if (/^\/serverless(\/|$)/.test(p)) {
      const t = new URLSearchParams(location.search).get("type");
      return SECTIONS[t]?.group === "App Services" ? "apps" : "serverless";
    }
    return null;
  }
  // The list page itself is the current page; a page inside it (a workload, a run) only opens it.
  const atTop = () => /^\/runs\/?$/.test(location.pathname) || !/\/(w|runs|new)\//.test(location.pathname + "/");

  const me = document.currentScript;
  me.parentElement.id ||= "sidenav";
  me.insertAdjacentHTML("beforebegin", `${DEFS}<nav aria-label="Console">${NAV.map(block).join("")}</nav>`);
  const root = me.parentElement;

  function setOpen(b, on, remember = true) {
    b.classList.toggle("open", on);
    b.querySelector(".nav-chev")?.setAttribute("aria-expanded", String(on));
    if (remember) { if (on) opened.add(b.dataset.block); else opened.delete(b.dataset.block); saveFolds(); }
  }
  // Cloud groups inside a section start folded, unless opened earlier in this tab.
  function setSub(s, on, remember = true) {
    s.classList.toggle("folded", !on);
    s.querySelector(".cloudfold-chev").setAttribute("aria-expanded", String(on));
    if (remember) { if (on) openSubs.add(s.dataset.sub); else openSubs.delete(s.dataset.sub); saveFolds(); }
  }
  for (const s of root.querySelectorAll(".cloudfold")) setSub(s, openSubs.has(s.dataset.sub), false);
  root.addEventListener("click", (e) => {
    const sc = e.target.closest(".cloudfold-chev");
    if (sc) { const s = sc.closest(".cloudfold"); setSub(s, s.classList.contains("folded")); return; }
    const c = e.target.closest(".nav-chev");
    if (c) { const b = c.closest(".nav-block"); setOpen(b, !b.classList.contains("open")); }
  });

  // A service kind (Cloud Run job) selects the list it belongs to.
  const sel = { nav: null, kind: null };
  function mark() {
    const p = location.pathname, u = new URLSearchParams(location.search);
    for (const [key, , , , kids] of NAV) {
      const b = root.querySelector(`[data-block="${key}"]`), top = b.querySelector("[data-nav]");
      const here = key === sel.nav;
      const hit = here ? kids.findIndex(([, , , when]) => when(p, u)) : -1;
      if (here) top.setAttribute("aria-current", atTop() && hit < 0 ? "page" : "true"); else top.removeAttribute("aria-current");
      b.classList.toggle("here", here);
      b.querySelectorAll(".nav-child").forEach((a, i) => {
        if (i === hit) a.setAttribute("aria-current", atTop() ? "page" : "true"); else a.removeAttribute("aria-current");
      });
      if (kids.length) setOpen(b, opened.has(key), false);
    }
    heading();
  }
  // A list page's heading is the current sidebar item, its name and icon (Functions Flex,
  // Azure); a page inside a list (a workload, a run) keeps its own title.
  function heading() {
    if (!atTop() || !sel.nav) return;
    const b = root.querySelector(`[data-block="${sel.nav}"]`);
    const cur = b?.querySelector('.nav-child[aria-current="page"]') || b?.querySelector('[data-nav][aria-current="page"]');
    const h1 = [...document.querySelectorAll("h1")].find((x) => !root.contains(x));
    if (!cur || !h1) return;
    const label = cur.querySelector("span").textContent;
    if (h1.textContent === label && h1.querySelector(".h1-ico")) return;
    const ico = document.createElement("span");
    ico.className = "h1-ico";
    ico.innerHTML = cur.querySelector("svg")?.outerHTML || "";   // constant sidebar SVG, no user data
    h1.replaceChildren(ico, label);
    document.title = `${label} · JFrog OIDC Console`;
  }
  // Pages draw their headings after load and on every filter change: put the sidebar name back each time.
  let headQueued = false;
  new MutationObserver(() => { if (!headQueued) { headQueued = true; queueMicrotask(() => { headQueued = false; heading(); }); } })
    .observe(document.body, { childList: true, subtree: true, characterData: true });
  function select(key) {
    const s = SECTIONS[key];
    sel.nav = s ? (s.group === "App Services" ? "apps" : "serverless") : key;
    sel.kind = s ? key : null;
    mark();
  }
  select(fromPath());
  requestAnimationFrame(() => root.classList.add("nav-ready"));   // no slide on first paint
  // List filters live in the address: follow them as they change.
  for (const fn of ["pushState", "replaceState"]) {
    const orig = history[fn].bind(history);
    history[fn] = (...a) => { const r = orig(...a); queueMicrotask(mark); return r; };
  }
  addEventListener("popstate", mark);

  // Back, on every page but Home: the page you came from, or one level up when you opened this one directly.
  const backTo = () => {
    const sameSite = document.referrer.startsWith(location.origin + "/");
    if (window.navigation?.canGoBack || (sameSite && history.length > 1)) return history.back();
    const up = location.pathname.replace(/\/+$/, "").split("/").slice(0, -1).join("/");
    location.assign(up || "/");
  };
  // It sits in the page, top left: in the breadcrumb row when the page shows one, else just above the heading.
  const back = document.createElement("button");
  back.type = "button"; back.className = "page-back"; back.title = "Back";
  back.innerHTML = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg><span>Back</span>';
  back.addEventListener("click", backTo);
  const row = document.createElement("div");
  row.className = "back-row";
  const place = () => {
    const main = document.querySelector("main");
    if (!main) return;
    if (location.pathname === "/" || location.pathname === "") { back.remove(); row.remove(); return; }
    const top = main.querySelector(".page-top");
    if (top && !top.hidden) {
      if (top.firstElementChild !== back) top.prepend(back);
      row.remove();
      return;
    }
    const h1 = main.querySelector("h1");
    if (!h1) return;
    const head = h1.closest("header") || h1;
    if (row.nextElementSibling !== head || back.parentNode !== row) { row.replaceChildren(back); head.before(row); }
  };
  const watch = () => {
    let queued = false;
    place();
    new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; place(); }); } })
      .observe(document.querySelector("main") || document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"] });
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", watch) : watch();
  // Service icons for other pages (list rows, Home): constant inline SVG, no user data.
  const icon = glyph;
  window.consoleNav = { icon, select, SECTIONS, sectionOfKind, section: (key) => SECTIONS[key] || null, current: () => sel.nav };

  // ------------------------------------------------------------------ filter bar
  // bar({placeholder, q, onQuery, pills: [{label, value, options: [[value, text]], onChange}]}) → element.
  // A pill reads "Cloud equals all"; clicking it opens its options, × clears it back to all.
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "text") n.textContent = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
    }
    n.append(...kids.flat().filter((x) => x !== null && x !== undefined && x !== false));
    return n;
  };
  let openPop = null;
  const closePop = () => { openPop?.remove(); openPop?._btn?.setAttribute("aria-expanded", "false"); openPop = null; };
  document.addEventListener("click", (e) => { if (openPop && !e.target.closest(".blade-pop, .blade-pill")) closePop(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openPop) { const b = openPop._btn; closePop(); b?.focus(); } });
  function pill(p) {
    const text = (p.options.find(([v]) => v === p.value) || [null, "all"])[1];
    const wrap = el("span", { class: `blade-pill${p.value ? " set" : ""}` });
    const btn = el("button", { type: "button", class: "blade-pill-btn", "aria-haspopup": "listbox", "aria-expanded": "false",
      onclick: () => {
        if (openPop?._btn === btn) return closePop();
        closePop();
        const pop = el("div", { class: "blade-pop", role: "listbox", "aria-label": p.label },
          [["", "All"], ...p.options].map(([v, t]) => el("button", { type: "button", role: "option", class: "blade-opt",
            "aria-selected": String(v === (p.value || "")), onclick: () => { closePop(); p.onChange(v); } }, t)));
        pop._btn = btn;
        wrap.append(pop);
        openPop = pop;
        btn.setAttribute("aria-expanded", "true");
        pop.querySelector('[aria-selected="true"]')?.focus();
      } }, `${p.label} equals `, el("b", { text }));
    wrap.append(btn);
    if (p.value) wrap.append(el("button", { type: "button", class: "blade-pill-x", "aria-label": `Clear ${p.label} filter`, onclick: () => p.onChange("") }, "×"));
    return wrap;
  }
  function bar({ placeholder = "Filter for any field…", q = "", onQuery, pills = [] }) {
    const box = el("input", { type: "search", class: "blade-search", placeholder, value: q, "aria-label": placeholder, spellcheck: "false",
      oninput: (e) => onQuery?.(e.target.value) });
    const wrapBox = el("span", { class: "blade-search-wrap" }, box);
    return el("div", { class: "blade-filters" }, onQuery ? wrapBox : null, pills.filter((p) => p.options.length > 1 || p.value).map(pill));
  }
  window.consoleBlade = { bar, el };

  // ------------------------------------------------------------------ sortable columns
  // Every list table (.blade-table) sorts by a click on its column heading: first click ascending, the next
  // descending. Rows sort within each tbody, so a table grouped by cloud
  // stays grouped; group and heading rows inside a tbody stay on top. The choice outlives redraws and refresh.
  const SORT_KEY = "console.sort";
  const sorts = (() => { try { return JSON.parse(sessionStorage.getItem(SORT_KEY)) || {}; } catch { return {}; } })();
  const sortable = (th) => th.scope === "col" && !th.classList.contains("sel") && !th.querySelector("input") && th.textContent.trim();
  const tableKey = (t) => `${location.pathname}|${[...(t.tHead?.rows[0]?.cells || [])].map((c) => c.textContent.trim()).join(",")}`;
  const cellValue = (td) => (td?.dataset.sort ?? td?.textContent ?? "").replace(/\s+/g, " ").trim();
  const DATE = /\b\d{4}\b.*\d{1,2}:\d{2}|^\d{4}-\d{2}-\d{2}/;
  function compare(a, b) {
    const blank = (v) => !v || v === "—";
    if (blank(a) || blank(b)) return blank(a) - blank(b);
    if (DATE.test(a) && DATE.test(b)) {
      const x = Date.parse(a.replace(/ ([A-Z]{3})$/, "")), y = Date.parse(b.replace(/ ([A-Z]{3})$/, ""));
      if (!Number.isNaN(x) && !Number.isNaN(y)) return x - y;
    }
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }
  function applySort(t) {
    const st = sorts[tableKey(t)];
    const width = t.tHead?.rows[0]?.cells.length || 0;
    for (const th of t.querySelectorAll("th")) {
      if (!sortable(th)) continue;
      if (!th.classList.contains("sortable")) { th.classList.add("sortable"); th.tabIndex = 0; }
      const on = st && th.cellIndex === st.col;
      const want = on ? (st.dir > 0 ? "ascending" : "descending") : null;
      if (th.getAttribute("aria-sort") !== want) want ? th.setAttribute("aria-sort", want) : th.removeAttribute("aria-sort");
    }
    if (!st) return;
    for (const body of t.tBodies) {
      const data = [...body.rows].filter((r) => r.cells.length === width && ![...r.cells].some((c) => c.tagName === "TH" || c.colSpan > 1));
      if (data.length < 2) continue;
      const sorted = [...data].sort((x, y) => st.dir * compare(cellValue(x.cells[st.col]), cellValue(y.cells[st.col])));
      if (sorted.every((r, i) => r === data[i])) continue;
      body.append(...sorted);
    }
  }
  const applyAll = () => document.querySelectorAll("table.blade-table").forEach(applySort);
  function onSortClick(e) {
    if (e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return;
    const th = e.target.closest("table.blade-table th.sortable");
    if (!th || e.target.closest("a, button, input")) return;
    e.preventDefault();
    const t = th.closest("table"), k = tableKey(t), cur = sorts[k];
    sorts[k] = { col: th.cellIndex, dir: cur && cur.col === th.cellIndex ? -cur.dir : 1 };
    try { sessionStorage.setItem(SORT_KEY, JSON.stringify(sorts)); } catch { /* memory only */ }
    applySort(t);
  }
  document.addEventListener("click", onSortClick);
  document.addEventListener("keydown", onSortClick);
  let sortQueued = false;
  new MutationObserver(() => { if (!sortQueued) { sortQueued = true; setTimeout(() => { sortQueued = false; applyAll(); }, 0); } })
    .observe(document.body, { childList: true, subtree: true });

  // Essentials grid: at most 4 columns, rows as even as the item count allows (4 → 2x2, 6 → 3x2, 8 → 4x2,
  // 9 → 3x3; 1–3 in one row). Among 4, 3 and 2 columns, the fewest empty cells without more rows than
  // columns; on a tie, the squarer. Set as --ess-cols on every Essentials block whenever its items change.
  function gridCols(n) {
    if (n <= 3) return Math.max(n, 1);
    let best = 4;
    for (const c of [4, 3, 2]) {
      const rows = Math.ceil(n / c), empty = rows * c - n;
      if (rows > c) continue;
      const bRows = Math.ceil(n / best), bEmpty = bRows * best - n;
      if (empty < bEmpty || (empty === bEmpty && Math.abs(c - rows) < Math.abs(best - bRows))) best = c;
    }
    return best;
  }
  let queued = false;
  const fit = () => {
    queued = false;
    for (const b of document.querySelectorAll(".ess-body, .essentials")) {
      const n = b.matches(".essentials") ? b.querySelectorAll(":scope > div").length : b.querySelectorAll(".ess-pair").length;
      const v = String(gridCols(n));
      if (n && b.style.getPropertyValue("--ess-cols") !== v) b.style.setProperty("--ess-cols", v);
    }
  };
  const start = () => {
    fit();
    new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(fit); } })
      .observe(document.body, { childList: true, subtree: true });
  };
  document.body ? start() : document.addEventListener("DOMContentLoaded", start);
  window.consoleBlade.gridCols = gridCols;
})();
