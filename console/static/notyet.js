// A cloud the console doesn't set up live yet: what it will do there, what it needs, and where to go now.
// Same page as the Kubernetes section's OpenShift.

import { h, BASE, CLOUDS, help } from "./core.js";

export const NOT_YET = {
  openshift: {
    will: "Find your Knative services, trust the cluster's service account issuer in JFrog, give each service a projected token for JFrog and prove the exchange: the same Set up and Verify as Google Cloud and Azure.",
    needs: ["A kubectl or oc context for the cluster",
      "The cluster's service account issuer published where JFrog can reach it (OIDC discovery)",
      "Knative Serving accepting projected service account token volumes (a config-features flag)"],
  },
};

const markOf = (c) => document.querySelector(`[data-cloud-link="${c}"] svg`)?.cloneNode(true) || null;

export function mountNotYet(page, cloud) {
  const label = CLOUDS[cloud];
  const info = NOT_YET[cloud];
  const ready = Object.entries(CLOUDS).filter(([c]) => !NOT_YET[c]);
  document.title = `JFrog OIDC Console · ${label}`;
  page.replaceChildren(
    h("header", { class: "page-header" },
      h("div", { class: "page-id" }, h("div", { class: "page-title" }, h("span", { class: "head-mark" }, markOf(cloud)), h("h1", {}, label)))),
    h("section", { class: "card not-yet" },
      h("div", { class: "card-head" }, h("h2", {}, `${label} isn't available yet`, info.will ? help(info.will) : null)),
      h("h3", {}, "What it will need"),
      h("ul", {}, info.needs.map((n) => h("li", {}, n))),
      h("h3", {}, "Available now"),
      h("p", { class: "ready-links" }, ready.map(([c, name]) => h("a", { href: `${BASE}/${c}` }, markOf(c), name)))));
}
