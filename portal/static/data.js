// What Home and Runs show: every workload, cluster and run, from both consoles' APIs (/serverless/api,
// /k8s/api) with the one session token, each placed under its cloud and menu item as the sidebar
// (shell.js) groups them. Serverless and Kubernetes are one product here; only the API differs.
// Official service icons (Google Cloud, Azure, AWS, Red Hat packs), inlined: no CDN.
export const MARK = {"gcp": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><g><g><polygon points=\"8.9 2.63 12.02 12 21.38 12 8.9 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /><polygon points=\"21.38 12 12.02 12 8.9 21.38 21.38 12\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 21.38 6.57 19.81 8.9 12 5.78 12 3.44 21.38\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 2.63 5.78 12 8.9 12 6.57 4.19 3.44 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /></g></g></svg>", "azure": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><g><g><g><g><path d=\"M16.932,11.578a8.448,8.448,0,0,1-7.95,5.59,8.15,8.15,0,0,1-2.33-.33,2.133,2.133,0,0,0,.18-.83c.01,0,.03.01.04.01a7.422,7.422,0,0,0,2.11.3,7.646,7.646,0,0,0,6.85-4.28l.01-.01Z\" fill=\"#32bedd\" /><path d=\"M3.582,14.068a2.025,2.025,0,0,0-.64.56,8.6,8.6,0,0,1-1.67-2.44l1.04.23v.26a.6.6,0,0,0,.47.59l.14.03a6.136,6.136,0,0,0,.62.73Z\" fill=\"#32bedd\" /><path d=\"M12.352.958a2.28,2.28,0,0,0-.27.81c-.02-.01-.05-.02-.07-.03a7.479,7.479,0,0,0-3.03-.63,7.643,7.643,0,0,0-5.9,2.8l-.29.06a.6.6,0,0,0-.48.58v.46l-1.02.19A8.454,8.454,0,0,1,8.982.268,8.6,8.6,0,0,1,12.352.958Z\" fill=\"#32bedd\" /><path d=\"M16.872,5.7l-1.09-.38a6.6,6.6,0,0,0-.72-1.16c-.02-.03-.04-.05-.05-.07a2.083,2.083,0,0,0,.72-.45A7.81,7.81,0,0,1,16.872,5.7Z\" fill=\"#32bedd\" /><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"#fff\" /><g><g><path d=\"M1.1,5.668l1.21-.23v6.55l-1.23-.27-.99-.22a.111.111,0,0,1-.09-.12v-5.4a.12.12,0,0,1,.09-.12Z\" fill=\"#a67af4\" /></g><g><g><g><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"url(#sl-aca-g0)\" /><path d=\"M8.586,3.3,2.878,4.378a.177.177,0,0,0-.14.175V12.68a.177.177,0,0,0,.137.174L8.581,14.1a.176.176,0,0,0,.21-.174V3.478A.175.175,0,0,0,8.619,3.3Z\" fill=\"url(#sl-aca-g1)\" /></g></g><polygon points=\"5.948 4.921 5.948 12.483 7.934 12.814 7.934 4.564 5.948 4.921\" fill=\"#b796f9\" opacity=\"0.5\" /><polygon points=\"3.509 5.329 3.509 11.954 5.238 12.317 5.238 5.031 3.509 5.329\" fill=\"#b796f9\" opacity=\"0.5\" /></g></g></g><path d=\"M16,2.048a1.755,1.755,0,1,1-1.76-1.76A1.756,1.756,0,0,1,16,2.048Z\" fill=\"#32bedd\" /><circle cx=\"4.65\" cy=\"15.973\" r=\"1.759\" fill=\"#32bedd\" /></g><path d=\"M18,6.689v3.844a.222.222,0,0,1-.133.2l-.766.316-3.07,1.268-.011,0a.126.126,0,0,1-.038,0,.1.1,0,0,1-.1-.1V5.234a.1.1,0,0,1,.054-.088l0,0,.019,0a.031.031,0,0,1,.019,0,.055.055,0,0,1,.034.008l.011,0,.012,0L17.05,6.2l.8.282A.213.213,0,0,1,18,6.689Z\" fill=\"#773adc\" /><path d=\"M13.959,5.14l-3.8.715a.118.118,0,0,0-.093.117v5.409a.118.118,0,0,0,.091.116l3.8.831a.115.115,0,0,0,.137-.09.109.109,0,0,0,0-.026V5.256a.117.117,0,0,0-.115-.118A.082.082,0,0,0,13.959,5.14Z\" fill=\"#a67af4\" /></g></g></svg>", "aks": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><polygon points=\"5.8 1.22 2.94 1.75 2.94 5.65 5.8 6.26 8.67 5.11 8.67 2.2 5.8 1.22\" fill=\"url(#kcp-aks-g)\"/><path d=\"M2.94,1.75v3.9l2.89.61v-5Zm1.22,3.6-.81-.16v-3l.81-.13Zm1.26.23-.93-.15V2l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"11.99 1.27 9.13 1.8 9.13 5.7 11.99 6.31 14.85 5.15 14.85 2.25 11.99 1.27\" fill=\"url(#kcp-aks-g)\"/><path d=\"M9.13,1.8V5.7L12,6.31v-5Zm1.21,3.6-.81-.16v-3l.81-.13Zm1.26.23-.93-.15V2.05l.93-.17Z\" fill=\"#341a6e\"/><polygon points=\"2.87 6.6 0.01 7.13 0.01 11.03 2.87 11.64 5.74 10.49 5.74 7.58 2.87 6.6\" fill=\"url(#kcp-aks-g)\"/><path d=\"M0,7.13V11l2.89.61v-5Zm1.21,3.61-.81-.17v-3l.81-.14ZM2.48,11l-.93-.15V7.38l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"9.04 6.56 6.18 7.09 6.18 10.99 9.04 11.61 11.9 10.45 11.9 7.54 9.04 6.56\" fill=\"url(#kcp-aks-g)\"/><path d=\"M6.18,7.09V11l2.88.61v-5ZM7.39,10.7l-.81-.17v-3l.81-.14Zm1.26.22-.93-.15V7.34l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"15.21 6.61 12.35 7.14 12.35 11.04 15.21 11.65 18.08 10.5 18.08 7.59 15.21 6.61\" fill=\"url(#kcp-aks-g)\"/><path d=\"M12.35,7.14V11l2.89.61v-5Zm1.22,3.61-.81-.17v-3l.81-.14Zm1.26.22-.93-.15V7.39l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"5.73 12.04 2.87 12.56 2.87 16.46 5.73 17.08 8.6 15.92 8.6 13.02 5.73 12.04\" fill=\"url(#kcp-aks-g)\"/><path d=\"M2.87,12.56v3.9l2.89.62V12Zm1.22,3.61L3.28,16V13l.81-.14Zm1.26.23-.93-.15V12.81l.93-.16Z\" fill=\"#341a6e\"/><polygon points=\"11.91 12.08 9.05 12.61 9.05 16.51 11.91 17.12 14.78 15.97 14.78 13.06 11.91 12.08\" fill=\"url(#kcp-aks-g)\"/><path d=\"M9.05,12.61v3.9l2.89.61v-5Zm1.22,3.61-.81-.17v-3l.81-.14Zm1.26.22-.93-.15V12.86l.93-.16Z\" fill=\"#341a6e\"/></svg>", "gke": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><polygon fill=\"#4285f4\" fill-rule=\"evenodd\" points=\"14.68 13.06 19.23 15.69 19.23 16.68 14.29 13.83 14.68 13.06\"/><polygon fill=\"#669df6\" fill-rule=\"evenodd\" points=\"9.98 13.65 4.77 16.66 4.45 15.86 9.53 12.92 9.98 13.65\"/><rect fill=\"#aecbfa\" x=\"11.55\" y=\"3.29\" width=\"0.86\" height=\"5.78\"/><path fill=\"#aecbfa\" fill-rule=\"evenodd\" d=\"M3.25,7V17L12,22l8.74-5V7L12,2Zm15.63,8.89L12,19.78,5.12,15.89V8.11L12,4.22l6.87,3.89v7.78Z\"/><polygon fill=\"#aecbfa\" fill-rule=\"evenodd\" points=\"11.98 11.5 15.96 9.21 11.98 6.91 8.01 9.21 11.98 11.5\"/><polygon fill=\"#669df6\" fill-rule=\"evenodd\" points=\"11.52 12.3 7.66 10.01 7.66 14.6 11.52 16.89 11.52 12.3\"/><polygon fill=\"#4285f4\" fill-rule=\"evenodd\" points=\"12.48 12.3 12.48 16.89 16.34 14.6 16.34 10.01 12.48 12.3\"/></svg>", "eks": "<svg class=\"mark\" viewBox=\"0 0 80 80\" aria-hidden=\"true\"><rect width=\"80\" height=\"80\" rx=\"12\" fill=\"#ED7100\"/><path d=\"M46.842,32.1990168 L40.013,40.1200132 L47.441,48.7840092 L44.669,48.7840092 L38,41.0000169 L38,49.0000169 L36,49.0000169 L36,32.0000169 L38,32.0000169 L38,39.0000169 L44.181,32.1990168 L46.842,32.1990168 Z M64,48.2340095 L56,43.4330117 L56,32.0000169 C56,31.6440171 55.812,31.3150172 55.504,31.1360173 L44,24.4260204 L44,14.7520248 L64,26.5710194 L64,48.2340095 Z M65.509,25.13902 L43.509,12.139026 C43.199,11.9560261 42.818,11.9540261 42.504,12.131026 C42.193,12.3090259 42,12.6410257 42,13.0000256 L42,25.0000201 C42,25.3550199 42.189,25.6840198 42.496,25.8640197 L54,32.5740166 L54,44.0000114 C54,44.3510113 54.185,44.6770111 54.486,44.857011 L64.486,50.8570083 C64.644,50.9520082 64.822,51 65,51 C65.17,51 65.34,50.9570082 65.493,50.8700083 C65.807,50.6930084 66,50.3600085 66,50 L66,26.0000196 C66,25.6460198 65.814,25.31902 65.509,25.13902 L65.509,25.13902 Z M40.445,66.863001 L17,54.3990067 L17,26.5710194 L37,14.7520248 L37,24.4510204 L26.463,31.1560173 C26.175,31.3400172 26,31.6580171 26,32.0000169 L26,49.0000091 C26,49.373009 26.208,49.7150088 26.538,49.8870087 L39.991,56.8870055 C40.28,57.0370055 40.624,57.0380055 40.912,56.8880055 L53.964,50.1440086 L61.996,54.9640064 L40.445,66.863001 Z M64.515,54.1420068 L54.515,48.1420095 C54.217,47.9640096 53.849,47.9520096 53.541,48.1120095 L40.455,54.8730065 L28,48.3930094 L28,32.5490167 L38.537,25.8440197 C38.825,25.6600198 39,25.3420199 39,25.0000201 L39,13.0000256 C39,12.6410257 38.808,12.3090259 38.496,12.131026 C38.184,11.9540261 37.802,11.9560261 37.491,12.139026 L15.491,25.13902 C15.187,25.31902 15,25.6460198 15,26.0000196 L15,55 C15,55.3690062 15.204,55.7090061 15.53,55.883006 L39.984,68.8830001 C40.131,68.961 40.292,69 40.453,69 C40.62,69 40.786,68.958 40.937,68.8750001 L64.484,55.875006 C64.797,55.7020061 64.993,55.3750062 65.0001416,55.0180064 C65.006,54.6600066 64.821,54.3260067 64.515,54.1420068 L64.515,54.1420068 Z\" fill=\"#FFFFFF\"/></svg>", "openshift": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#EE0000\" d=\"M21.665,11.812c-0.11-1.377-0.476-2.724-1.08-3.966L24,6.599c-0.268-0.556-0.585-1.092-0.943-1.595 l-1.601,0.583c-3.534-4.95-10.412-6.098-15.363-2.565c-3.144,2.244-4.883,5.972-4.582,9.823l1.604-0.584 c0.051,0.615,0.153,1.224,0.305,1.822L0,15.335c0.338,1.339,0.922,2.604,1.721,3.731l1.812-0.659 c3.526,4.95,10.398,6.106,15.349,2.58c1.555-1.107,2.796-2.6,3.599-4.332c0.802-1.715,1.144-3.61,0.991-5.497L21.665,11.812z M16.925,9.177c0.687,1.227,0.998,2.629,0.895,4.032l1.809-0.657c-0.063,0.856-0.282,1.694-0.646,2.471 c-1.67,3.584-5.928,5.138-9.514,3.472c-0.782-0.365-1.491-0.87-2.092-1.49l-1.813,0.66c-0.979-1.01-1.64-2.285-1.903-3.667 l3.426-1.242c-0.121-0.624-0.159-1.262-0.111-1.896H6.97l-1.604,0.583c0.294-3.932,3.72-6.881,7.652-6.587 c0.868,0.065,1.716,0.288,2.504,0.658V5.508c0.778,0.364,1.483,0.867,2.082,1.483l1.599-0.582c0.002,0.002,0.004,0.003,0.006,0.005 c0.441,0.454,0.82,0.965,1.128,1.518L16.925,9.177z\"/></svg>", "run": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><g><g><polygon points=\"8.9 2.63 12.02 12 21.38 12 8.9 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /><polygon points=\"21.38 12 12.02 12 8.9 21.38 21.38 12\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 21.38 6.57 19.81 8.9 12 5.78 12 3.44 21.38\" fill-rule=\"evenodd\" fill=\"#4285f4\" /><polygon points=\"3.44 2.63 5.78 12 8.9 12 6.57 4.19 3.44 2.63\" fill=\"#aecbfa\" fill-rule=\"evenodd\" /></g></g></svg>", "fn": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><g><g><g><polygon points=\"2 15.56 5 18 5 6 2 8.43 2 15.56\" fill=\"#669df6\" /><polygon points=\"2 10 5 8 5 6 2 10\" fill=\"#4285f4\" /><polygon points=\"5 18 5 16 2 14 5 18\" fill=\"#4285f4\" /><polygon points=\"8 20 2 14 2 18 6 22 8 20\" fill=\"#aecbfa\" /></g></g><polygon points=\"5.99 2.01 2 6 2.01 10 7.99 4.01 5.99 2.01\" fill=\"#aecbfa\" /><g><g><polygon points=\"22 8.45 19 6.01 19 18.01 22 15.59 22 8.45\" fill=\"#669df6\" /><polygon points=\"22 14.01 19 16.01 19 18.01 22 14.01\" fill=\"#4285f4\" /><polygon points=\"19 6.01 19 8.01 22 10.01 19 6.01\" fill=\"#4285f4\" /><polygon points=\"16 4.01 22 10.01 22 6.01 18 2.01 16 4.01\" fill=\"#aecbfa\" /></g></g><polygon points=\"18.01 22 22 18.01 21.99 14.01 16.01 20 18.01 22\" fill=\"#aecbfa\" /><circle cx=\"8\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /><circle cx=\"12\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /><circle cx=\"15.99\" cy=\"12\" r=\"1\" fill=\"#aecbfa\" /></g></svg>", "aca": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><g><g><g><g><path d=\"M16.932,11.578a8.448,8.448,0,0,1-7.95,5.59,8.15,8.15,0,0,1-2.33-.33,2.133,2.133,0,0,0,.18-.83c.01,0,.03.01.04.01a7.422,7.422,0,0,0,2.11.3,7.646,7.646,0,0,0,6.85-4.28l.01-.01Z\" fill=\"#32bedd\" /><path d=\"M3.582,14.068a2.025,2.025,0,0,0-.64.56,8.6,8.6,0,0,1-1.67-2.44l1.04.23v.26a.6.6,0,0,0,.47.59l.14.03a6.136,6.136,0,0,0,.62.73Z\" fill=\"#32bedd\" /><path d=\"M12.352.958a2.28,2.28,0,0,0-.27.81c-.02-.01-.05-.02-.07-.03a7.479,7.479,0,0,0-3.03-.63,7.643,7.643,0,0,0-5.9,2.8l-.29.06a.6.6,0,0,0-.48.58v.46l-1.02.19A8.454,8.454,0,0,1,8.982.268,8.6,8.6,0,0,1,12.352.958Z\" fill=\"#32bedd\" /><path d=\"M16.872,5.7l-1.09-.38a6.6,6.6,0,0,0-.72-1.16c-.02-.03-.04-.05-.05-.07a2.083,2.083,0,0,0,.72-.45A7.81,7.81,0,0,1,16.872,5.7Z\" fill=\"#32bedd\" /><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"#fff\" /><g><g><path d=\"M1.1,5.668l1.21-.23v6.55l-1.23-.27-.99-.22a.111.111,0,0,1-.09-.12v-5.4a.12.12,0,0,1,.09-.12Z\" fill=\"#a67af4\" /></g><g><g><g><path d=\"M10.072,11.908l2.54.56L8.672,14.1c-.02,0-.03.01-.05.01a.154.154,0,0,1-.15-.15V3.448a.154.154,0,0,1,.15-.15.09.09,0,0,1,.05.01l4.46,1.56-3.05.57a.565.565,0,0,0-.44.54v5.4A.537.537,0,0,0,10.072,11.908Z\" fill=\"url(#sl-aca-g0)\" /><path d=\"M8.586,3.3,2.878,4.378a.177.177,0,0,0-.14.175V12.68a.177.177,0,0,0,.137.174L8.581,14.1a.176.176,0,0,0,.21-.174V3.478A.175.175,0,0,0,8.619,3.3Z\" fill=\"url(#sl-aca-g1)\" /></g></g><polygon points=\"5.948 4.921 5.948 12.483 7.934 12.814 7.934 4.564 5.948 4.921\" fill=\"#b796f9\" opacity=\"0.5\" /><polygon points=\"3.509 5.329 3.509 11.954 5.238 12.317 5.238 5.031 3.509 5.329\" fill=\"#b796f9\" opacity=\"0.5\" /></g></g></g><path d=\"M16,2.048a1.755,1.755,0,1,1-1.76-1.76A1.756,1.756,0,0,1,16,2.048Z\" fill=\"#32bedd\" /><circle cx=\"4.65\" cy=\"15.973\" r=\"1.759\" fill=\"#32bedd\" /></g><path d=\"M18,6.689v3.844a.222.222,0,0,1-.133.2l-.766.316-3.07,1.268-.011,0a.126.126,0,0,1-.038,0,.1.1,0,0,1-.1-.1V5.234a.1.1,0,0,1,.054-.088l0,0,.019,0a.031.031,0,0,1,.019,0,.055.055,0,0,1,.034.008l.011,0,.012,0L17.05,6.2l.8.282A.213.213,0,0,1,18,6.689Z\" fill=\"#773adc\" /><path d=\"M13.959,5.14l-3.8.715a.118.118,0,0,0-.093.117v5.409a.118.118,0,0,0,.091.116l3.8.831a.115.115,0,0,0,.137-.09.109.109,0,0,0,0-.026V5.256a.117.117,0,0,0-.115-.118A.082.082,0,0,0,13.959,5.14Z\" fill=\"#a67af4\" /></g></g></svg>", "func": "<svg class=\"mark\" viewBox=\"0 0 18 18\" aria-hidden=\"true\"><g><path d=\"M2.37,7.475H3.2a.267.267,0,0,1,.267.267v6.148a.533.533,0,0,1-.533.533H2.1a0,0,0,0,1,0,0V7.741a.267.267,0,0,1,.267-.267Z\" transform=\"translate(12.507 16.705) rotate(134.919)\" fill=\"#50e6ff\" /><path d=\"M2.325,3.6h.833a.267.267,0,0,1,.267.267v6.583a0,0,0,0,1,0,0H2.591a.533.533,0,0,1-.533-.533V3.865A.267.267,0,0,1,2.325,3.6Z\" transform=\"translate(5.759 0.114) rotate(44.919)\" fill=\"#1490df\" /></g><g><path d=\"M14.53,7.475h.833a.533.533,0,0,1,.533.533v6.148a.267.267,0,0,1-.267.267H14.8a.267.267,0,0,1-.267-.267V7.475a0,0,0,0,1,0,0Z\" transform=\"translate(12.223 -7.555) rotate(45.081)\" fill=\"#50e6ff\" /><path d=\"M15.108,3.6h.833a0,0,0,0,1,0,0v6.583a.267.267,0,0,1-.267.267h-.833a.267.267,0,0,1-.267-.267V4.131a.533.533,0,0,1,.533-.533Z\" transform=\"translate(31.022 1.222) rotate(135.081)\" fill=\"#1490df\" /></g><path d=\"M8.459,9.9H4.87a.193.193,0,0,1-.2-.181.166.166,0,0,1,.018-.075L8.991,1.13a.206.206,0,0,1,.186-.106h4.245a.193.193,0,0,1,.2.181.165.165,0,0,1-.035.1L8.534,7.966h4.928a.193.193,0,0,1,.2.181.176.176,0,0,1-.052.122L5.421,16.788c-.077.046-.624.5-.356-.189h0Z\" fill=\"url(#sl-func-g0)\" /></svg>", "aws_sl": "<svg class=\"mark\" viewBox=\"0 0 80 80\" aria-hidden=\"true\"><rect width=\"80\" height=\"80\" fill=\"#ED7100\"/><path d=\"M28.0075352,66 L15.5907274,66 L29.3235885,37.296 L35.5460249,50.106 L28.0075352,66 Z M30.2196674,34.553 C30.0512768,34.208 29.7004629,33.989 29.3175745,33.989 L29.3145676,33.989 C28.9286723,33.99 28.5778583,34.211 28.4124746,34.558 L13.097944,66.569 C12.9495999,66.879 12.9706487,67.243 13.1550766,67.534 C13.3374998,67.824 13.6582439,68 14.0020416,68 L28.6420072,68 C29.0299071,68 29.3817234,67.777 29.5481094,67.428 L37.563706,50.528 C37.693006,50.254 37.6920037,49.937 37.5586944,49.665 L30.2196674,34.553 Z M64.9953491,66 L52.6587274,66 L32.866809,24.57 C32.7014253,24.222 32.3486067,24 31.9617091,24 L23.8899822,24 L23.8990031,14 L39.7197081,14 L59.4204149,55.429 C59.5857986,55.777 59.9386172,56 60.3255148,56 L64.9953491,56 L64.9953491,66 Z M65.9976745,54 L60.9599868,54 L41.25928,12.571 C41.0938963,12.223 40.7410777,12 40.3531778,12 L22.89768,12 C22.3453987,12 21.8963569,12.447 21.8953545,12.999 L21.884329,24.999 C21.884329,25.265 21.9885708,25.519 22.1780103,25.707 C22.3654452,25.895 22.6200358,26 22.8866544,26 L31.3292417,26 L51.1221625,67.43 C51.2885485,67.778 51.6393624,68 52.02626,68 L65.9976745,68 C66.5519605,68 67,67.552 67,67 L67,55 C67,54.448 66.5519605,54 65.9976745,54 L65.9976745,54 Z\" fill=\"#FFFFFF\"/></svg>", "openshift_sl": "<svg class=\"mark\" viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"#EE0000\" d=\"M21.665,11.812c-0.11-1.377-0.476-2.724-1.08-3.966L24,6.599c-0.268-0.556-0.585-1.092-0.943-1.595 l-1.601,0.583c-3.534-4.95-10.412-6.098-15.363-2.565c-3.144,2.244-4.883,5.972-4.582,9.823l1.604-0.584 c0.051,0.615,0.153,1.224,0.305,1.822L0,15.335c0.338,1.339,0.922,2.604,1.721,3.731l1.812-0.659 c3.526,4.95,10.398,6.106,15.349,2.58c1.555-1.107,2.796-2.6,3.599-4.332c0.802-1.715,1.144-3.61,0.991-5.497L21.665,11.812z M16.925,9.177c0.687,1.227,0.998,2.629,0.895,4.032l1.809-0.657c-0.063,0.856-0.282,1.694-0.646,2.471 c-1.67,3.584-5.928,5.138-9.514,3.472c-0.782-0.365-1.491-0.87-2.092-1.49l-1.813,0.66c-0.979-1.01-1.64-2.285-1.903-3.667 l3.426-1.242c-0.121-0.624-0.159-1.262-0.111-1.896H6.97l-1.604,0.583c0.294-3.932,3.72-6.881,7.652-6.587 c0.868,0.065,1.716,0.288,2.504,0.658V5.508c0.778,0.364,1.483,0.867,2.082,1.483l1.599-0.582c0.002,0.002,0.004,0.003,0.006,0.005 c0.441,0.454,0.82,0.965,1.128,1.518L16.925,9.177z\"/></svg>"};

export const CLOUD_NAMES = { gcp: "Google Cloud", azure: "Azure", aws: "AWS", openshift: "OpenShift" };
// Menu item → [cloud, label, page, mark, group]: the same items as the sidebar (shell.js). Each cloud
// holds Serverless (one item per workload kind) and Kubernetes (one item per cluster platform).
const sl = (cloud, kind, label, mark) => [cloud, label, `/serverless/${cloud}?type=${kind}`, mark, "Serverless"];
const k8s = (cloud, p, label) => [cloud, label, `/k8s/clusters/${p}`, p, "Kubernetes"];
export const SECTIONS = {
  gcp_run_job: sl("gcp", "gcp_run_job", "Cloud Run jobs", "run"),
  gcp_run_service: sl("gcp", "gcp_run_service", "Cloud Run services", "run"),
  gcp_run_function: sl("gcp", "gcp_run_function", "Cloud Run functions", "fn"),
  gcp_run_worker_pool: sl("gcp", "gcp_run_worker_pool", "Cloud Run worker pools", "run"),
  gke: k8s("gcp", "gke", "GKE clusters"),
  azure_container_app_job: sl("azure", "azure_container_app_job", "Container Apps jobs", "aca"),
  azure_container_app: sl("azure", "azure_container_app", "Container Apps", "aca"),
  azure_functions_aca: sl("azure", "azure_functions_aca", "Functions on Container Apps", "func"),
  azure_functions_premium: sl("azure", "azure_functions_premium", "Functions Premium", "func"),
  azure_functions_flex: sl("azure", "azure_functions_flex", "Functions Flex Consumption", "func"),
  aks: k8s("azure", "aks", "AKS clusters"),
  eks: k8s("aws", "eks", "EKS clusters"),
  openshift: k8s("openshift", "openshift", "OpenShift clusters"),
};
// Serverless families: the services of one product share a heading on Home ([family, short name]).
export const FAMILY = {
  azure_container_app_job: ["Container Apps", "Jobs"], azure_container_app: ["Container Apps", "Apps"],
  azure_functions_aca: ["Functions", "On Container Apps"], azure_functions_premium: ["Functions", "Premium / Dedicated"],
  azure_functions_flex: ["Functions", "Flex Consumption"],
  gcp_run_job: ["Cloud Run", "Jobs"], gcp_run_service: ["Cloud Run", "Services"],
  gcp_run_function: ["Cloud Run", "Functions"], gcp_run_worker_pool: ["Cloud Run", "Worker pools"],
};
const sectionOfKind = (k) => (SECTIONS[k] ? k : null);
const markOfKind = (k) => k === "gcp_run_function" ? "fn" : k.startsWith("gcp_") ? "run" : k.startsWith("azure_functions") ? "func"
  : k.startsWith("azure_") ? "aca" : k.startsWith("aws_") ? "aws_sl" : "openshift_sl";

// Run status → [pill class, words]. Serverless and Kubernetes runs use slightly different words.
const RUN = { succeeded: ["ok", "Succeeded"], failed: ["bad", "Failed"], cancelled: ["bad", "Cancelled"],
  refused: ["bad", "Refused"], interrupted: ["bad", "Interrupted"], running: ["info", "Running"], waiting_approval: ["warn", "Waiting for approval"],
  prepared: ["warn", "Waiting for approval"] };
export const runPill = (s) => RUN[s] || ["", String(s || "unknown").replaceAll("_", " ")];
export const LIVE_STATES = new Set(["running", "waiting_approval", "prepared"]);
// Cluster state → [pill class, words].
const CLUSTER = { working: ["ok", "OIDC working"], attention: ["bad", "Needs attention"], not_proven: ["bad", "Not proven"],
  approval: ["warn", "Awaiting approval"], unverified: ["warn", "Not verified"], running: ["info", "Running"],
  not_installed: ["", "Not set up"], unread: ["", "Not read"] };

function token() {
  const m = document.querySelector('meta[name="console-token"]');
  if (m) { try { localStorage.setItem("console-session-token", m.content); } catch { /* memory only */ } return m.content; }
  try { return localStorage.getItem("console-session-token"); } catch { return null; }
}
const TOKEN = token();

async function get(path) {
  const r = await fetch(path, { headers: { "X-Console-Token": TOKEN || "" }, credentials: "omit", cache: "no-store" });
  if (!r.ok) throw new Error(r.status === 401 ? "signed-out" : `HTTP ${r.status}`);
  return r.json();
}

// Serverless Prove: starts a prove-only run of the saved configuration and answers its page.
export async function startProve(cloud, name) {
  const headers = { "X-Console-Token": TOKEN || "", "Content-Type": "application/json" };
  const n = encodeURIComponent(name);
  const c = await get(`/serverless/api/configs/${n}`);
  const r = await fetch("/serverless/api/runs", { method: "POST", headers, credentials: "omit", cache: "no-store",
    body: JSON.stringify({ config: c.config || c, steps: ["prove"] }) });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return `/serverless/${cloud}/w/${n}/runs/${encodeURIComponent(body.run_id)}`;
}

// Admin: forget one finished run (its record and log). Active runs answer 409.
export async function removeRun(path) {
  const r = await fetch(path, { method: "DELETE", headers: { "X-Console-Token": TOKEN || "" }, credentials: "omit", cache: "no-store" });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const b = await r.json(); msg = b.error?.message || b.message || b.error || msg; } catch { /* keep status */ }
    throw new Error(r.status === 401 ? "signed-out" : String(msg));
  }
  return r.json();
}

export function h(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined && v !== false) {
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v; else n.setAttribute(k, v);
  }
  n.append(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
  return n;
}
export const markEl = (k) => { const s = h("span", { class: "dash-mark" }); s.innerHTML = MARK[k] || ""; return s; };  // constant SVG only
// Stored in UTC, shown in the browser's own time zone.
export const when = (t) => { const d = new Date(t || ""); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString([],
  { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }); };
const words = (s) => String(s || "").toLowerCase().replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());

const VERIFY = { succeeded: ["ok", "Working"], failed: ["bad", "Failed"], cancelled: ["bad", "Cancelled"] };

// Serverless: workloads and their runs.
async function serverless() {
  const [{ configs }, { runs }, schema] = await Promise.all([get("/serverless/api/configs"), get("/serverless/api/runs"),
    get("/serverless/api/schema")]);
  const label = (k) => schema.kinds?.[k]?.label || k;
  const cloudOf = (k) => String(k).split("_")[0];
  // Status comes from the last onboarding run; Verified from the last Verify (runs come newest first).
  const latest = new Map(), verified = new Map(), proved = new Map();
  for (const r of runs) {
    if (!r.verify_only && !r.prove_only && !latest.has(r.name)) latest.set(r.name, r);
    if (r.verify && !verified.has(r.name)) verified.set(r.name, r);
    if (r.prove && ["succeeded", "failed"].includes(r.prove.status) && !proved.has(r.name)) proved.set(r.name, r);
  }
  const items = configs.map((c) => {
    const last = latest.get(c.name), ver = verified.get(c.name), pr = proved.get(c.name);
    const runHref = (r) => `/serverless/${cloudOf(c.kind)}/w/${encodeURIComponent(c.name)}/runs/${encodeURIComponent(r.id)}`;
    return { kind: "workload", cloud: cloudOf(c.kind), section: sectionOfKind(c.kind), name: c.name, type: label(c.kind),
      mark: markOfKind(c.kind), href: `/serverless/${cloudOf(c.kind)}/w/${encodeURIComponent(c.name)}`,
      typeHref: `/serverless/${cloudOf(c.kind)}?type=${encodeURIComponent(c.kind)}`,
      ok: last?.status === "succeeded", state: last ? runPill(last.status) : ["", "No runs"],
      detail: last ? `last run ${when(last.created)}` : null,
      verified: ver ? { ok: ver.verify.status === "succeeded", state: VERIFY[ver.verify.status] || ["warn", "Checking"],
        when: when(ver.verify.finished || ver.created), href: runHref(ver) } : null,
      proof: pr ? { ok: pr.prove.status === "succeeded", state: pr.prove.status === "succeeded" ? ["ok", "OIDC proven"] : ["bad", "Not proven"],
        when: when(pr.prove.finished || pr.created), href: runHref(pr) } : null };
  });
  // A run cut off before its workload was recorded has no name or kind, so no page to open.
  const runRows = runs.map((r) => {
    const known = Boolean(r.name && r.kind);
    return { cloud: known ? cloudOf(r.kind) : null, section: known ? sectionOfKind(r.kind) : null,
      name: known ? r.name : "Unknown workload", type: known ? label(r.kind) : "No workload recorded",
      mark: known ? markOfKind(r.kind) : null, status: r.status, created: r.created, id: r.id, area: "serverless", api: `/serverless/api/runs/${encodeURIComponent(r.id)}`,
      href: known ? `/serverless/${cloudOf(r.kind)}/w/${encodeURIComponent(r.name)}/runs/${encodeURIComponent(r.id)}` : null,
      nameHref: known ? `/serverless/${cloudOf(r.kind)}/w/${encodeURIComponent(r.name)}` : null,
      typeHref: known ? `/serverless/${cloudOf(r.kind)}?type=${encodeURIComponent(r.kind)}` : null };
  });
  return { items, runs: runRows };
}

// Kubernetes: clusters and their runs.
async function kubernetes() {
  const [fleet, { configs }, { runs }] = await Promise.all([get("/k8s/api/fleet"), get("/k8s/api/configs"), get("/k8s/api/runs")]);
  const platform = new Map(configs.map((c) => [c.name, c.platform || "aks"]));
  const items = (fleet.clusters || []).map((c) => {
    const p = c.cloud || platform.get(c.name) || "aks";
    return { kind: "cluster", cloud: SECTIONS[p]?.[0] || null, section: p, name: c.name,
      type: SECTIONS[p]?.[1].replace(/s$/, "") || "Cluster", mark: p, href: `/k8s/clusters/${p}/${encodeURIComponent(c.name)}`,
      typeHref: `/k8s/clusters/${p}`,
      ok: c.state === "working", state: CLUSTER[c.state] || ["", words(c.state)],
      detail: c.problem || (c.verified_at ? `verified ${when(c.verified_at)}` : c.cluster || "") };
  });
  // A run records its configuration's name, not its platform: one whose configuration was deleted has
  // no known cloud and no page to open, so it says so instead of guessing.
  const runRows = runs.map((r) => {
    const p = platform.get(r.config) || null;
    const known = Boolean(p);
    return { cloud: p ? SECTIONS[p]?.[0] || null : null, section: p, name: r.config || r.cluster || "Unknown cluster",
      type: `${(p && SECTIONS[p]?.[1].replace(/s$/, "")) || "Cluster (configuration deleted)"} · ${words(r.name || r.kind)}`, mark: p,
      status: r.status, created: r.created, id: r.id, area: "kubernetes", api: `/k8s/api/runs/${encodeURIComponent(r.id)}`,
      href: known ? `/k8s/clusters/${p}/${encodeURIComponent(r.config)}/runs/${encodeURIComponent(r.id)}` : null,
      nameHref: known ? `/k8s/clusters/${p}/${encodeURIComponent(r.config)}` : null, typeHref: known ? `/k8s/clusters/${p}` : null };
  });
  // Proof: each cluster's latest real Prove run, read in full (its steps hold the evidence).
  const lastProve = new Map();
  for (const r of runs) if (r.name === "prove" && r.config && r.request?.parameters?.DryRun !== true && !lastProve.has(r.config) && !["prepared", "refused"].includes(r.status)) lastProve.set(r.config, r);
  const proofs = await Promise.all(items.map(async (it) => {
    const r = lastProve.get(it.name);
    let run = null;
    if (r) { try { run = await get(`/k8s/api/runs/${encodeURIComponent(r.id)}`); } catch { run = null; } }
    return proofOf(it, run?.run || run);
  }));
  items.forEach((it, i) => { it.proof = proofs[i]; });
  return { items, runs: runRows };
}

// One cluster's proof (its Proof column), from the steps of its Prove run; null before the first.
function proofOf(it, run) {
  if (!run || !run.result) return null;
  const step = Object.fromEntries((run.result.steps || []).map((s) => [s.id, s]));
  const good = (id) => ["ok", "done"].includes(step[id]?.status);
  const http = (id) => step[id]?.facts?.jfrog?.status;
  const claim = (ok, text) => ({ ok, text });
  const claims = {
    secret: claim(good("no-secret") && good("no-keys"), good("no-secret") ? "None" : "Found"),
    exchange: claim(http("granted-1") === 200 && http("denied") >= 400 && http("granted-2") === 200,
      [http("granted-1"), http("denied"), http("granted-2")].map((x) => x ?? "—").join(" → ")),
    pull: claim(good("pull") && step.pull?.facts?.pulled !== false, good("pull") ? "Pulled" : "Failed"),
    refused: claim(good("refused"), good("refused") ? "Refused" : "Not refused"),
    restored: claim(good("repull"), good("repull") ? "Pulled" : "Failed"),
  };
  const ok = run.status === "succeeded" && Object.values(claims).every((c) => c.ok);
  const detail = `Stored secret ${claims.secret.text} · JFrog exchange ${claims.exchange.text} · pull with OIDC ${claims.pull.text}`
    + ` · without trust ${claims.refused.text} · trust restored ${claims.restored.text}`;
  return { ok, state: ok ? ["ok", "OIDC proven"] : ["bad", "Not proven"], claims, detail,
    when: when(run.finished || run.created), href: `${it.href}/runs/${encodeURIComponent(run.id)}` };
}

// Infrastructure and JFrog OIDC: every deploy, destroy and apply run on the Setup pages or through a
// stack's stack-setup pipeline. Each row opens its log on the page it was run from.
const SETUP_STATE = { done: "succeeded", failed: "failed", running: "running" };
const SETUP_ACTION = { "plan-apply": "Deploy plan", "plan-destroy": "Destroy plan", apply: "Apply", forget: "Forget" };
// Each from its own mount: /setup lists only infrastructure runs, /oidc only JFrog OIDC runs.
function setupRuns(page, area, typeOf) {
  return async () => {
    const { jobs } = await get(`${page}/api/jobs`);
    const iso = (t) => t ? new Date(t * 1000).toISOString().replace(/\.\d+Z$/, "Z") : "";
    return { items: [], runs: jobs.map((j) => ({ cloud: j.cloud, section: null, name: j.title, mark: j.cloud,
      type: `${typeOf(j)} · ${SETUP_ACTION[j.action] || words(j.action)}`,
      status: SETUP_STATE[j.state] || j.state, created: iso(j.started), id: j.id, area,
      api: `${page}/api/jobs/${encodeURIComponent(j.id)}`, href: `${page}?job=${encodeURIComponent(j.id)}`,
      typeHref: j.cloud ? `${page}?cloud=${j.cloud}` : page })) };
  };
}
const infrastructure = setupRuns("/setup", "infrastructure", (j) => `${j.kind} infrastructure`);
const jfrogOidc = setupRuns("/oidc", "oidc", () => "JFrog OIDC");

// Archived: the runs of workloads, clusters and stacks whose resources are gone, kept 7 days and read only.
const DAY = 86400000;
const left = (ms) => { const d = Math.max(0, Math.ceil((ms - Date.now()) / DAY)); return d === 1 ? "deleted in 1 day" : `deleted in ${d} days`; };
async function archived() {
  const [sv, k8, inf, oi] = await Promise.all([get("/serverless/api/archive"), get("/k8s/api/archive"),
    get("/setup/api/jobs/archive"), get("/oidc/api/jobs/archive")]);
  const iso = (t) => t ? new Date(t * 1000).toISOString().replace(/\.\d+Z$/, "Z") : "";
  const rows = [];
  for (const e of sv.entries || []) {
    const cloud = String(e.kind || "").split("_")[0] || null;
    for (const r of e.run_rows || []) rows.push({ cloud, section: sectionOfKind(e.kind), name: e.name, mark: markOfKind(e.kind),
      type: `Serverless workload · ${left(e.expires_at * 1000)}`, status: r.status, created: r.created, id: r.id, area: "archived", api: null,
      href: `/serverless/${cloud}/w/${encodeURIComponent(e.name)}/runs/${encodeURIComponent(r.id)}` });
  }
  for (const e of k8.entries || []) {
    const p = e.kind || "aks";
    for (const r of e.run_rows || []) rows.push({ cloud: SECTIONS[p]?.[0] || null, section: p, name: e.name, mark: p,
      type: `${SECTIONS[p]?.[1].replace(/s$/, "") || "Cluster"} · ${left(e.expires_at * 1000)}`, status: r.status, created: r.created,
      id: r.id, area: "archived", api: null, href: `/k8s/clusters/${p}/${encodeURIComponent(e.name)}/runs/${encodeURIComponent(r.id)}` });
  }
  for (const [page, res] of [["/setup", inf], ["/oidc", oi]]) for (const j of res.jobs || []) rows.push({ cloud: j.cloud, section: null,
    name: j.title, mark: j.cloud, type: `${page === "/oidc" ? "JFrog OIDC" : `${j.kind} infrastructure`} · ${left(j.expires_at * 1000)}`,
    status: SETUP_STATE[j.state] || j.state, created: iso(j.started), id: j.id, area: "archived", api: null, href: `${page}?job=${encodeURIComponent(j.id)}` });
  return { items: [], runs: rows };
}

// Stacks with nothing standing (never deployed, destroyed, gone): listed only under Runs › Archived,
// never on the live Infrastructure or JFrog OIDC pages. Deploy opens the stack on its page's archived view.
const STACK_CLOUD = { Azure: "azure", "Google Cloud": "gcp" };
export async function archivedStacks() {
  const out = [];
  for (const [page, area] of [["/setup", "Infrastructure"], ["/oidc", "JFrog OIDC"]]) {
    const { stacks = [] } = await get(`${page}/api/stacks`);
    for (const s of stacks) {
      if ((page === "/oidc") !== (s.kind === "JFrog OIDC")) continue;
      if (s.status?.deployed || s.busy || s.plan || s.job) continue;
      const partly = Boolean(s.status?.deleted && Object.keys(s.status.outputs || {}).length);
      out.push({ cloud: STACK_CLOUD[s.group] || null, name: s.title, stack: s.id, area,
        status: partly ? "Partly deleted" : "Not deployed", href: `${page}?view=archived&q=${encodeURIComponent(s.id)}` });
    }
  }
  return out;
}

// Everything, newest run first. A source that fails is reported, not shown as empty.
const SOURCES = [["Serverless", serverless], ["Kubernetes", kubernetes], ["Infrastructure", infrastructure], ["JFrog OIDC", jfrogOidc], ["Archived", archived]];
export async function loadAll() {
  const results = await Promise.allSettled(SOURCES.map(([, f]) => f()));
  const ok = results.filter((r) => r.status === "fulfilled").map((r) => r.value);
  const errors = results.map((r, i) => r.status === "rejected" ? { source: SOURCES[i][0], message: r.reason?.message } : null)
    .filter(Boolean);
  return { items: ok.flatMap((v) => v.items), runs: ok.flatMap((v) => v.runs).sort((a, b) => String(b.created).localeCompare(String(a.created))),
    errors, signedOut: errors.some((e) => e.message === "signed-out") };
}
