// The console's one navigation, on every page: Home, Infrastructure, then each cloud with its
// Serverless services and its Kubernetes clusters, then Runs. Serverless and Kubernetes are one
// product, grouped by cloud. A classic script placed inside <aside class="sidenav">: it writes the
// menu where it stands, before the page's own module runs, so the first paint already has it.
// Pages refine the open item with consoleNav.select().
(() => {
  // Official service icons and line icons, inlined: no CDN.
  const ICON = {"home": "<svg class=\"ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M3 10.5 12 3l9 7.5\"/><path d=\"M5 9.5V20h5v-6h4v6h5V9.5\"/></svg>", "infra": "<svg class=\"ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><rect x=\"3\" y=\"4\" width=\"18\" height=\"7\" rx=\"1.5\"/><rect x=\"3\" y=\"13\" width=\"18\" height=\"7\" rx=\"1.5\"/><path d=\"M7 7.5h.01M7 16.5h.01\"/></svg>", "runs": "<svg class=\"ico\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path d=\"M5 6h14M5 12h14M5 18h9\"/><path d=\"M17 15.5l4 2.5-4 2.5z\"/></svg>", "run": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><g><g><polygon points=\"8.9 2.63 12.02 12 21.38 12 8.9 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /><polygon points=\"21.38 12 12.02 12 8.9 21.38 21.38 12\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 21.38 6.57 19.81 8.9 12 5.78 12 3.44 21.38\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 2.63 5.78 12 8.9 12 6.57 4.19 3.44 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /></g></g></svg>", "gke": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><polygon fill=\"#4285f4\" fill-rule=\"evenodd\" points=\"14.68 13.06 19.23 15.69 19.23 16.68 14.29 13.83 14.68 13.06\"/><polygon fill=\"#669df6\" fill-rule=\"evenodd\" points=\"9.98 13.65 4.77 16.66 4.45 15.86 9.53 12.92 9.98 13.65\"/><rect fill=\"#aecbfa\" x=\"11.55\" y=\"3.29\" width=\"0.86\" height=\"5.78\"/><path fill=\"#aecbfa\" fill-rule=\"evenodd\" d=\"M3.25,7V17L12,22l8.74-5V7L12,2Zm15.63,8.89L12,19.78,5.12,15.89V8.11L12,4.22l6.87,3.89v7.78Z\"/><polygon fill=\"#aecbfa\" fill-rule=\"evenodd\" points=\"11.98 11.5 15.96 9.21 11.98 6.91 8.01 9.21 11.98 11.5\"/><polygon fill=\"#669df6\" fill-rule=\"evenodd\" points=\"11.52 12.3 7.66 10.01 7.66 14.6 11.52 16.89 11.52 12.3\"/><polygon fill=\"#4285f4\" fill-rule=\"evenodd\" points=\"12.48 12.3 12.48 16.89 16.34 14.6 16.34 10.01 12.48 12.3\"/></svg>", "aca": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><g><g><g><g><path d=\"M16.932,11.578a8.448,8.448,0,0,1-7.95,5.59,8.15,8.15,0,0,1-2.33-.33,2.133,2.133,0,0,0,.18-.83c.01,0,.03.01.04.01a7.422,7.422,0,0,0,2.11.3,7.646,7.646,0,0,0,6.85-4.28l.01-.01Z\" fill=\"#32bedd\" /><path d=\"M3.582,14.068a2.025,2.025,0,0,0-.64.56,8.6,8.6,0,0,1-1.67-2.44l1.04.23v.26a.6.6,0,0,0,.47.59l.14.03a6.136,6.136,0,0,0,.62.73Z\" fill=\"#32bedd\" /><path d=\"M12.352.958a2.28,2.28,0,0,0-.27.81c-.02-.01-.05-.02-.07-.03a7.479,7.479,0,0,0-3.03-.63,7.643,7.643,0,0,0-5.9,2.8l-.29.06a.6.6,0,0,0-.48.58v.46l-1.02.19A8.454,8.454,0,0,1,8.982.268,8.6,8.6,0,0,1,12.352.958Z\" fill=\"#32bedd\" /><path d=\"M16.872,5.7l-1.09-.38a6.6,6.6,0,0,0-.72-1.16c-.02-.03-.04-.05-.05-.07a2.083,2.083,0,0,0,.72-.45A7.81,7.81,0,0,1,16.872,5.7Z\" fill=\"#32bedd\" /><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"#fff\" /><g><g><path d=\"M1.1,5.668l1.21-.23v6.55l-1.23-.27-.99-.22a.111.111,0,0,1-.09-.12v-5.4a.12.12,0,0,1,.09-.12Z\" fill=\"#a67af4\" /></g><g><g><g><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"url(#sl-aca-g0)\" /><path d=\"M8.586,3.3,2.878,4.378a.177.177,0,0,0-.14.175V12.68a.177.177,0,0,0,.137.174L8.581,14.1a.176.176,0,0,0,.21-.174V3.478A.175.175,0,0,0,8.619,3.3Z\" fill=\"url(#sl-aca-g1)\" /></g></g><polygon points=\"5.948 4.921 5.948 12.483 7.934 12.814 7.934 4.564 5.948 4.921\" fill=\"#b796f9\" opacity=\"0.5\" /><polygon points=\"3.509 5.329 3.509 11.954 5.238 12.317 5.238 5.031 3.509 5.329\" fill=\"#b796f9\" opacity=\"0.5\" /></g></g></g><path d=\"M16,2.048a1.755,1.755,0,1,1-1.76-1.76A1.756,1.756,0,0,1,16,2.048Z\" fill=\"#32bedd\" /><circle cx=\"4.65\" cy=\"15.973\" r=\"1.759\" fill=\"#32bedd\" /></g><path d=\"M18,6.689v3.844a.222.222,0,0,1-.133.2l-.766.316-3.07,1.268-.011,0a.126.126,0,0,1-.038,0,.1.1,0,0,1-.1-.1V5.234a.1.1,0,0,1,.054-.088l0,0,.019,0a.031.031,0,0,1,.019,0,.055.055,0,0,1,.034.008l.011,0,.012,0L17.05,6.2l.8.282A.213.213,0,0,1,18,6.689Z\" fill=\"#773adc\" /><path d=\"M13.959,5.14l-3.8.715a.118.118,0,0,0-.093.117v5.409a.118.118,0,0,0,.091.116l3.8.831a.115.115,0,0,0,.137-.09.109.109,0,0,0,0-.026V5.256a.117.117,0,0,0-.115-.118A.082.082,0,0,0,13.959,5.14Z\" fill=\"#a67af4\" /></g></g></svg>", "func": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><g><path d=\"M2.37,7.475H3.2a.267.267,0,0,1,.267.267v6.148a.533.533,0,0,1-.533.533H2.1a0,0,0,0,1,0,0V7.741a.267.267,0,0,1,.267-.267Z\" transform=\"translate(12.507 16.705) rotate(134.919)\" fill=\"#50e6ff\" /><path d=\"M2.325,3.6h.833a.267.267,0,0,1,.267.267v6.583a0,0,0,0,1,0,0H2.591a.533.533,0,0,1-.533-.533V3.865A.267.267,0,0,1,2.325,3.6Z\" transform=\"translate(5.759 0.114) rotate(44.919)\" fill=\"#1490df\" /></g><g><path d=\"M14.53,7.475h.833a.533.533,0,0,1,.533.533v6.148a.267.267,0,0,1-.267.267H14.8a.267.267,0,0,1-.267-.267V7.475a0,0,0,0,1,0,0Z\" transform=\"translate(12.223 -7.555) rotate(45.081)\" fill=\"#50e6ff\" /><path d=\"M15.108,3.6h.833a0,0,0,0,1,0,0v6.583a.267.267,0,0,1-.267.267h-.833a.267.267,0,0,1-.267-.267V4.131a.533.533,0,0,1,.533-.533Z\" transform=\"translate(31.022 1.222) rotate(135.081)\" fill=\"#1490df\" /></g><path d=\"M8.459,9.9H4.87a.193.193,0,0,1-.2-.181.166.166,0,0,1,.018-.075L8.991,1.13a.206.206,0,0,1,.186-.106h4.245a.193.193,0,0,1,.2.181.165.165,0,0,1-.035.1L8.534,7.966h4.928a.193.193,0,0,1,.2.181.176.176,0,0,1-.052.122L5.421,16.788c-.077.046-.624.5-.356-.189h0Z\" fill=\"url(#sl-func-g0)\" /></svg>", "aks": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><polygon points=\"5.8 1.22 2.94 1.75 2.94 5.65 5.8 6.26 8.67 5.11 8.67 2.2 5.8 1.22\" fill=\"url(#kcp-aks-g)\"/><path d=\"M2.94,1.75v3.9l2.89.61v-5Zm1.22,3.6-.81-.16v-3l.81-.13Zm1.26.23-.93-.15V2l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"11.99 1.27 9.13 1.8 9.13 5.7 11.99 6.31 14.85 5.15 14.85 2.25 11.99 1.27\" fill=\"url(#kcp-aks-g)\"/><path d=\"M9.13,1.8V5.7L12,6.31v-5Zm1.21,3.6-.81-.16v-3l.81-.13Zm1.26.23-.93-.15V2.05l.93-.17Z\" fill=\"#341a6e\"/><polygon points=\"2.87 6.6 0.01 7.13 0.01 11.03 2.87 11.64 5.74 10.49 5.74 7.58 2.87 6.6\" fill=\"url(#kcp-aks-g)\"/><path d=\"M0,7.13V11l2.89.61v-5Zm1.21,3.61-.81-.17v-3l.81-.14ZM2.48,11l-.93-.15V7.38l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"9.04 6.56 6.18 7.09 6.18 10.99 9.04 11.61 11.9 10.45 11.9 7.54 9.04 6.56\" fill=\"url(#kcp-aks-g)\"/><path d=\"M6.18,7.09V11l2.88.61v-5ZM7.39,10.7l-.81-.17v-3l.81-.14Zm1.26.22-.93-.15V7.34l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"15.21 6.61 12.35 7.14 12.35 11.04 15.21 11.65 18.08 10.5 18.08 7.59 15.21 6.61\" fill=\"url(#kcp-aks-g)\"/><path d=\"M12.35,7.14V11l2.89.61v-5Zm1.22,3.61-.81-.17v-3l.81-.14Zm1.26.22-.93-.15V7.39l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"5.73 12.04 2.87 12.56 2.87 16.46 5.73 17.08 8.6 15.92 8.6 13.02 5.73 12.04\" fill=\"url(#kcp-aks-g)\"/><path d=\"M2.87,12.56v3.9l2.89.62V12Zm1.22,3.61L3.28,16V13l.81-.14Zm1.26.23-.93-.15V12.81l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"11.91 12.08 9.05 12.61 9.05 16.51 11.91 17.12 14.78 15.97 14.78 13.06 11.91 12.08\" fill=\"url(#kcp-aks-g)\"/><path d=\"M9.05,12.61v3.9l2.89.61v-5Zm1.22,3.61-.81-.17v-3l.81-.14Zm1.26.22-.93-.15V12.86l.93-.16Z\" fill=\"#341a6e\"/></svg>", "fn": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><g><g><g><polygon points=\"2 15.56 5 18 5 6 2 8.43 2 15.56\" fill=\"#669df6\" /><polygon points=\"2 10 5 8 5 6 2 10\" fill=\"#4285f4\" /><polygon points=\"5 18 5 16 2 14 5 18\" fill=\"#4285f4\" /><polygon points=\"8 20 2 14 2 18 6 22 8 20\" fill=\"#aecbfa\" /></g></g><polygon points=\"5.99 2.01 2 6 2.01 10 7.99 4.01 5.99 2.01\" fill=\"#aecbfa\" /><g><g><polygon points=\"22 8.45 19 6.01 19 18.01 22 15.59 22 8.45\" fill=\"#669df6\" /><polygon points=\"22 14.01 19 16.01 19 18.01 22 14.01\" fill=\"#4285f4\" /><polygon points=\"19 6.01 19 8.01 22 10.01 19 6.01\" fill=\"#4285f4\" /><polygon points=\"16 4.01 22 10.01 22 6.01 18 2.01 16 4.01\" fill=\"#aecbfa\" /></g></g><polygon points=\"18.01 22 22 18.01 21.99 14.01 16.01 20 18.01 22\" fill=\"#aecbfa\" /><circle cx=\"8\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /><circle cx=\"12\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /><circle cx=\"15.99\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /></g></svg>"};
  const DEFS = "<svg class=\"mark-defs\" width=\"0\" height=\"0\" aria-hidden=\"true\" focusable=\"false\"><defs><linearGradient id=\"kcp-aks-g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"0\"><stop offset=\"0\" stop-color=\"#b77af4\"/><stop offset=\"1\" stop-color=\"#773adc\"/></linearGradient><linearGradient id=\"sl-aca-g0\" x1=\"8.798\" y1=\"8.703\" x2=\"14.683\" y2=\"8.703\" gradientUnits=\"userSpaceOnUse\"><stop offset=\"0.001\" stop-color=\"#773adc\" /><stop offset=\"1\" stop-color=\"#552f99\" /></linearGradient><linearGradient id=\"sl-aca-g1\" x1=\"5.764\" y1=\"3.777\" x2=\"5.764\" y2=\"13.78\" gradientUnits=\"userSpaceOnUse\"><stop offset=\"0\" stop-color=\"#a67af4\" /><stop offset=\"0.999\" stop-color=\"#773adc\" /></linearGradient><linearGradient id=\"sl-func-g0\" x1=\"-175.993\" y1=\"-343.723\" x2=\"-175.993\" y2=\"-359.232\" gradientTransform=\"translate(212.573 370.548) scale(1.156 1.029)\" gradientUnits=\"userSpaceOnUse\"><stop offset=\"0\" stop-color=\"#fea11b\" /><stop offset=\"0.284\" stop-color=\"#fea51a\" /><stop offset=\"0.547\" stop-color=\"#feb018\" /><stop offset=\"0.8\" stop-color=\"#ffc314\" /><stop offset=\"1\" stop-color=\"#ffd70f\" /></linearGradient></defs></svg>";
  // A Serverless item is one workload kind (its list is /serverless/<cloud>?type=<kind>); a Kubernetes
  // item is one cluster platform. [key, label, href, icon, attrs]
  const sl = (cloud, kind, label, icon) => [kind, label, `/serverless/${cloud}?type=${kind}`, icon];
  const k8s = (p, label) => [p, label, `/k8s/clusters/${p}`, p, { "data-cloud": p }];
  const CLOUDS = [
    ["Google Cloud", "gcp", [
      ["Serverless", [sl("gcp", "gcp_run_job", "Cloud Run jobs", "run"), sl("gcp", "gcp_run_service", "Cloud Run services", "run"),
        sl("gcp", "gcp_run_function", "Cloud Run functions", "fn")]],
      ["Kubernetes", [k8s("gke", "GKE clusters")]]]],
    ["Azure", "azure", [
      ["Serverless", [sl("azure", "azure_container_app_job", "Container Apps jobs", "aca"),
        sl("azure", "azure_container_app", "Container Apps", "aca"),
        sl("azure", "azure_functions_aca", "Functions on Container Apps", "func"),
        sl("azure", "azure_functions_premium", "Functions Premium", "func"),
        sl("azure", "azure_functions_flex", "Functions Flex Consumption", "func")]],
      ["Kubernetes", [k8s("aks", "AKS clusters")]]]],
  ];
  const TOP = [["home", "Home", "/", "home"], ["infra", "Infrastructure", "/setup", "infra"]];
  const BOTTOM = [["runs", "Runs", "/runs", "runs"]];

  // key → {key, label, href, cloud, group}: what a page needs for its title and breadcrumb.
  const SECTIONS = {};
  for (const [cloud, id, groups] of CLOUDS) {
    for (const [group, items] of groups) for (const [key, label, href] of items) SECTIONS[key] = { key, label, href, cloud, cloudId: id, group };
    SECTIONS[`serverless-${id}`] = { key: `serverless-${id}`, label: "Serverless", href: `/serverless/${id}`, cloud, cloudId: id, group: null };
  }
  const sectionOfKind = (kind) => (SECTIONS[kind] ? kind : null);

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const link = ([key, label, href, icon, attrs], cls = "") => {
    const extra = Object.entries(attrs || {}).map(([k, v]) => ` ${k}="${esc(v)}"`).join("");
    return `<a class="nav-item${cls}" href="${esc(href)}" data-section data-nav="${key}"${extra}>${ICON[icon]}<span>${esc(label)}</span></a>`;
  };
  function fromPath() {
    const p = location.pathname, type = new URLSearchParams(location.search).get("type");
    if (p === "/") return "home";
    if (/^\/setup(\/|$)/.test(p)) return "infra";
    if (/^\/runs(\/|$)/.test(p)) return "runs";
    const s = p.match(/^(?:\/serverless)?\/(gcp|azure)(\/|$)/);
    if (s) return SECTIONS[type]?.cloudId === s[1] ? type : `serverless-${s[1]}`;
    const k = p.match(/^(?:\/k8s)?\/clusters\/(gke|aks)(\/|$)/);
    return k ? k[1] : null;
  }
  // The list page itself is the current page; a page inside it (a workload, a cluster, a run) only opens it.
  const atTop = () => !/\/(w|runs|new)\/|\/clusters\/[a-z]+\/[^/]+/.test(location.pathname + "/");

  const html = [DEFS];
  html.push(`<nav aria-label="Home">${TOP.map((i) => link(i)).join("")}</nav>`);
  for (const [cloud, , groups] of CLOUDS) {
    html.push(`<nav aria-label="${esc(cloud)}"><div class="nav-group">${esc(cloud)}</div>`);
    for (const [group, items] of groups) html.push(`<div class="nav-sub">${esc(group)}</div>`, ...items.map((i) => link(i, " nested")));
    html.push("</nav>");
  }
  html.push(`<nav aria-label="Runs">${BOTTOM.map((i) => link(i)).join("")}</nav>`);
  html.push('<nav class="subnav" id="k8s-sub" aria-label="Clusters" hidden></nav>');
  const me = document.currentScript;
  me.parentElement.id ||= "sidenav";
  me.insertAdjacentHTML("beforebegin", html.join(""));

  function select(key) {
    for (const a of document.querySelectorAll(".sidenav [data-nav]")) {
      if (a.dataset.nav === key) a.setAttribute("aria-current", atTop() ? "page" : "true");
      else a.removeAttribute("aria-current");
    }
  }
  select(fromPath());
  window.consoleNav = { select, SECTIONS, sectionOfKind, section: (key) => SECTIONS[key] || null, current: fromPath };
})();
