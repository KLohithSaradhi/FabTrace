(() => {
  "use strict";

  document.addEventListener("DOMContentLoaded", () => {
    const initialRecipe = window.FABTRACE_RECIPE;
    if (!initialRecipe || typeof window.cytoscape !== "function") {
      document.getElementById("validation-panel").innerHTML =
        "<strong>The graph editor could not load.</strong> Check the Cytoscape.js CDN connection.";
      return;
    }

    let recipe = structuredClone(initialRecipe);
    let isNew = Boolean(window.FABTRACE_IS_NEW);
    let connectSource = null;
    let connectMode = false;
    let providerDetectionSequence = 0;

    // Planner v1 still requires this legacy recipe field. Keep its synthetic
    // compatibility value out of product-authoring decisions until planning
    // derives lead time from the selected provider and order quantity.
    Object.values(recipe.nodes).forEach((node) => {
      if (node.kind === "material" && !(Number(node.lead_time_days) > 0)) {
        node.lead_time_days = 1;
      }
    });

    const graphElement = document.getElementById("product-graph");
    const propertyPanel = document.getElementById("property-panel");
    const deleteButton = document.getElementById("delete-selected");
    const connectButton = document.getElementById("connect-nodes");
    const connectHelp = document.getElementById("connect-help");
    const productIdInput = document.getElementById("product-id");
    const productNameInput = document.getElementById("product-name");
    const descriptionInput = document.getElementById("product-description");
    const statusElement = document.getElementById("document-status");
    const actionStatus = document.getElementById("editor-action-status");

    const elements = [];
    Object.entries(recipe.nodes).forEach(([nodeId, node]) => {
      elements.push({
        group: "nodes",
        data: { id: nodeId, label: node.name, kind: node.kind },
        position: node.position || { x: 0, y: 0 },
      });
      (node.requires || []).forEach((requirement) => {
        elements.push({
          group: "edges",
          data: {
            id: `${requirement.node_id}::${nodeId}`,
            source: requirement.node_id,
            target: nodeId,
            quantity: requirement.quantity,
            unit: requirement.unit,
          },
        });
      });
    });

    const cy = cytoscape({
      container: graphElement,
      elements,
      minZoom: 0.35,
      maxZoom: 2.5,
      wheelSensitivity: 0.18,
      boxSelectionEnabled: true,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#18342d",
            "border-color": "#6de5b1",
            "border-width": 2,
            color: "#eff7f3",
            label: "data(label)",
            "font-size": 11,
            "font-weight": 700,
            "text-wrap": "wrap",
            "text-max-width": 105,
            "text-valign": "center",
            width: 118,
            height: 58,
            shape: "round-rectangle",
          },
        },
        {
          selector: 'node[kind = "material"]',
          style: { "background-color": "#193348", "border-color": "#63b3ed" },
        },
        {
          selector: 'node[kind = "process"]',
          style: { "background-color": "#342b1a", "border-color": "#f3b95f" },
        },
        {
          selector: 'node[kind = "capability"]',
          style: { "background-color": "#2d2340", "border-color": "#b794f4" },
        },
        {
          selector: 'node[kind = "output"]',
          style: { "background-color": "#173d31", "border-color": "#6de5b1", "border-width": 3 },
        },
        {
          selector: "edge",
          style: {
            width: 2,
            "line-color": "#577068",
            "target-arrow-color": "#6de5b1",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            label: "data(quantity)",
            color: "#91aaa1",
            "font-size": 9,
            "text-background-color": "#0d1917",
            "text-background-opacity": 1,
            "text-background-padding": 3,
          },
        },
        {
          selector: ":selected",
          style: { "overlay-color": "#6de5b1", "overlay-opacity": 0.14, "overlay-padding": 8 },
        },
        {
          selector: ".connect-source",
          style: { "border-color": "#ffffff", "border-width": 4 },
        },
      ],
      layout: { name: elements.length ? "preset" : "grid", fit: true, padding: 60 },
    });

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function safeNodeId(kind) {
      let index = 1;
      while (recipe.nodes[`${kind}-${index}`]) index += 1;
      return `${kind}-${index}`;
    }

    function syncRequirements() {
      Object.values(recipe.nodes).forEach((node) => { node.requires = []; });
      cy.edges().forEach((edge) => {
        const target = recipe.nodes[edge.target().id()];
        if (!target) return;
        target.requires.push({
          node_id: edge.source().id(),
          quantity: Number(edge.data("quantity")),
          unit: String(edge.data("unit") || "unit"),
        });
      });
      Object.values(recipe.nodes).forEach((node) => {
        node.requires.sort((left, right) => left.node_id.localeCompare(right.node_id));
      });
    }

    function buildRecipe() {
      recipe.product_id = productIdInput.value.trim();
      recipe.product_name = productNameInput.value.trim();
      recipe.description = descriptionInput.value.trim();
      recipe.synthetic = true;
      cy.nodes().forEach((node) => {
        if (recipe.nodes[node.id()]) {
          const position = node.position();
          recipe.nodes[node.id()].position = { x: position.x, y: position.y };
        }
      });
      syncRequirements();
      const outputs = Object.entries(recipe.nodes)
        .filter(([, node]) => node.kind === "output")
        .map(([nodeId]) => nodeId);
      recipe.output_node_id = outputs.length === 1 ? outputs[0] : null;
      return recipe;
    }

    function mermaidSource() {
      const current = buildRecipe();
      const ids = Object.keys(current.nodes);
      const aliases = Object.fromEntries(ids.map((id, index) => [id, `n${index}`]));
      const lines = ["flowchart LR"];
      ids.forEach((id) => {
        const label = current.nodes[id].name.replaceAll('"', "'");
        lines.push(`  ${aliases[id]}["${label}"]`);
      });
      Object.entries(current.nodes).forEach(([targetId, node]) => {
        node.requires.forEach((requirement) => {
          if (aliases[requirement.node_id]) {
            lines.push(
              `  ${aliases[requirement.node_id]} -->|${requirement.quantity} ${requirement.unit}| ${aliases[targetId]}`
            );
          }
        });
      });
      return lines.join("\n");
    }

    function updatePreviews() {
      document.getElementById("json-preview").textContent = JSON.stringify(buildRecipe(), null, 2);
      document.getElementById("mermaid-preview").textContent = mermaidSource();
    }

    function markChanged() {
      statusElement.textContent = "unsaved draft";
      statusElement.className = "document-status status-draft";
      actionStatus.textContent = "";
      actionStatus.className = "editor-action-status";
      updatePreviews();
    }

    function reportAction(message, type = "") {
      actionStatus.textContent = message;
      actionStatus.className = `editor-action-status ${type}`.trim();
    }

    function renderEmptyProperties() {
      propertyPanel.innerHTML = `
        <div class="empty-properties">
          <p class="eyebrow">Properties</p>
          <h2>Select an element</h2>
          <p>Choose a node or dependency edge to edit its details.</p>
        </div>`;
      deleteButton.disabled = true;
    }

    function renderNodeProperties(nodeElement) {
      const nodeId = nodeElement.id();
      const node = recipe.nodes[nodeId];
      if (!node) return renderEmptyProperties();
      propertyPanel.innerHTML = `
        <div class="property-heading"><p class="eyebrow">Node properties</p><h2>${escapeHtml(node.name)}</h2><code>${escapeHtml(nodeId)}</code></div>
        <label>Name<input id="node-name" value="${escapeHtml(node.name)}"></label>
        <label>Type<select id="node-kind">
          ${["material", "process", "capability", "output"].map((kind) => `<option value="${kind}" ${node.kind === kind ? "selected" : ""}>${kind}</option>`).join("")}
        </select></label>
        <label>Output unit<input id="node-unit" value="${escapeHtml(node.unit || "")}" placeholder="wafer, kg, unit"></label>
        <label>Duration in days<input id="node-duration" type="number" min="0" step="0.1" value="${node.duration_days ?? ""}" placeholder="Required for processes"></label>
        <label>ETO reference ID<input id="node-eto" value="${escapeHtml(node.eto_reference_id || "")}" placeholder="For example N99"></label>
        <label>Provider candidates<input id="node-providers" value="${escapeHtml((node.provider_candidates || []).join(", "))}" placeholder="Detected from the ETO reference"></label>
        <button id="detect-node-providers" class="secondary-button" type="button">Refresh providers from ETO</button>
        <p id="provider-detection-status" class="field-help">Candidates are source relationships, not a reliability or quality ranking. The field remains editable.</p>
        <p class="field-help">Separate provider IDs with commas. The first candidate is selected by Planner v1.</p>`;
      deleteButton.disabled = false;

      const updateNode = () => {
        node.name = document.getElementById("node-name").value.trim() || nodeId;
        node.kind = document.getElementById("node-kind").value;
        node.unit = document.getElementById("node-unit").value.trim() || null;
        node.lead_time_days = node.kind === "material"
          ? (Number(node.lead_time_days) > 0 ? Number(node.lead_time_days) : 1)
          : null;
        const duration = document.getElementById("node-duration").value;
        node.duration_days = duration === "" ? null : Number(duration);
        node.eto_reference_id = document.getElementById("node-eto").value.trim() || null;
        node.provider_candidates = document.getElementById("node-providers").value
          .split(",").map((value) => value.trim()).filter(Boolean);
        nodeElement.data({ label: node.name, kind: node.kind });
        const heading = propertyPanel.querySelector(".property-heading h2");
        if (heading) heading.textContent = node.name;
        markChanged();
      };
      propertyPanel.querySelectorAll("input, select").forEach((input) => {
        input.addEventListener("input", updateNode);
      });
      document.getElementById("node-eto").addEventListener("change", () => {
        detectProviders(nodeElement, node, true);
      });
      document.getElementById("detect-node-providers").addEventListener(
        "click",
        () => detectProviders(nodeElement, node, true)
      );
      if (
        ["material", "process"].includes(node.kind)
        && node.eto_reference_id
        && node.provider_candidates.length === 0
      ) {
        detectProviders(nodeElement, node, false);
      }
    }

    function renderEdgeProperties(edge) {
      const source = edge.source().id();
      const target = edge.target().id();
      propertyPanel.innerHTML = `
        <div class="property-heading"><p class="eyebrow">Dependency properties</p><h2>${escapeHtml(source)} → ${escapeHtml(target)}</h2></div>
        <label>Required quantity<input id="edge-quantity" type="number" min="0.000001" step="any" value="${escapeHtml(edge.data("quantity"))}"></label>
        <label>Unit<input id="edge-unit" value="${escapeHtml(edge.data("unit"))}"></label>
        <p class="field-help">The unit must match the prerequisite node's output unit.</p>`;
      deleteButton.disabled = false;
      propertyPanel.querySelectorAll("input").forEach((input) => {
        input.addEventListener("input", () => {
          const quantity = Number(document.getElementById("edge-quantity").value);
          edge.data("quantity", quantity);
          edge.data("unit", document.getElementById("edge-unit").value.trim());
          markChanged();
        });
      });
    }

    function selectElement(element) {
      deleteButton.disabled = false;
      if (element.isNode()) renderNodeProperties(element);
      else renderEdgeProperties(element);
    }

    function resetConnectMode() {
      connectMode = false;
      connectSource = null;
      cy.nodes().removeClass("connect-source");
      connectButton.classList.remove("active");
      connectHelp.textContent = "Select a node or edge to configure it.";
    }

    cy.on("select", "node, edge", (event) => selectElement(event.target));
    cy.on("unselect", "node, edge", () => {
      if (cy.$(":selected").length === 0) renderEmptyProperties();
    });
    cy.on("dragfree", "node", markChanged);
    cy.on("tap", "node", (event) => {
      if (!connectMode) return;
      const node = event.target;
      if (!connectSource) {
        connectSource = node.id();
        node.addClass("connect-source");
        connectHelp.textContent = `Prerequisite ${connectSource} selected. Now choose the dependent node.`;
        return;
      }
      const target = node.id();
      if (target === connectSource) {
        connectHelp.textContent = "A node cannot depend on itself. Choose another node.";
        return;
      }
      const edgeId = `${connectSource}::${target}`;
      if (cy.getElementById(edgeId).length) {
        connectHelp.textContent = "That dependency already exists.";
        return;
      }
      cy.add({
        group: "edges",
        data: {
          id: edgeId,
          source: connectSource,
          target,
          quantity: 1,
          unit: recipe.nodes[connectSource].unit || "unit",
        },
      });
      resetConnectMode();
      markChanged();
    });
    cy.on("tap", (event) => {
      if (event.target === cy) {
        cy.$(":selected").unselect();
        renderEmptyProperties();
      }
    });

    document.querySelectorAll("[data-add-kind]").forEach((button) => {
      button.addEventListener("click", () => {
        const kind = button.dataset.addKind;
        const nodeId = safeNodeId(kind);
        const position = cy.extent();
        recipe.nodes[nodeId] = {
          name: nodeId.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()),
          kind,
          unit: kind === "output" ? "unit" : null,
          lead_time_days: kind === "material" ? 1 : null,
          duration_days: kind === "process" ? 1 : null,
          eto_reference_id: null,
          provider_candidates: [],
          position: { x: (position.x1 + position.x2) / 2, y: (position.y1 + position.y2) / 2 },
          requires: [],
        };
        const added = cy.add({
          group: "nodes",
          data: { id: nodeId, label: recipe.nodes[nodeId].name, kind },
          position: recipe.nodes[nodeId].position,
        });
        cy.$(":selected").unselect();
        added.select();
        cy.animate({ center: { eles: added }, duration: 180 });
        markChanged();
      });
    });

    connectButton.addEventListener("click", () => {
      if (connectMode) return resetConnectMode();
      connectMode = true;
      connectButton.classList.add("active");
      connectHelp.textContent = "Choose the prerequisite node first.";
    });
    document.getElementById("layout-graph").addEventListener("click", () => {
      cy.layout({ name: "breadthfirst", directed: true, padding: 55, spacingFactor: 1.15 }).run();
      setTimeout(markChanged, 50);
    });
    document.getElementById("fit-graph").addEventListener("click", () => cy.fit(undefined, 55));
    deleteButton.addEventListener("click", () => {
      const selected = cy.$(":selected");
      if (!selected.length) return;
      const selectedNodes = selected.nodes();
      const description = selectedNodes.length
        ? `${selectedNodes.length === 1 ? `node ${selectedNodes.id()}` : `${selectedNodes.length} nodes`} and connected dependencies`
        : "selected dependency";
      if (!window.confirm(`Delete ${description}?`)) return;
      selectedNodes.forEach((node) => {
        delete recipe.nodes[node.id()];
      });
      selected.remove();
      renderEmptyProperties();
      resetConnectMode();
      markChanged();
    });

    [productIdInput, productNameInput, descriptionInput].forEach((input) => {
      input.addEventListener("input", markChanged);
    });

    function formatError(payload) {
      if (!payload) return "Unknown error";
      if (typeof payload.detail === "string") return payload.detail;
      if (Array.isArray(payload.detail)) {
        return payload.detail.map((item) => `${item.loc.join(".")}: ${item.msg}`).join("; ");
      }
      return JSON.stringify(payload);
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, options);
      const payload = await response.json();
      if (!response.ok) throw new Error(formatError(payload));
      return payload;
    }

    async function detectProviders(nodeElement, node, force) {
      if (!["material", "process"].includes(node.kind)) return;
      const capabilityId = String(node.eto_reference_id || "").trim();
      const detectionStatus = document.getElementById("provider-detection-status");
      if (!capabilityId) {
        if (detectionStatus) {
          detectionStatus.textContent = "Enter an ETO reference ID to detect providers.";
        }
        return;
      }
      if (!force && node.provider_candidates.length > 0) return;
      const requestSequence = ++providerDetectionSequence;
      if (detectionStatus) detectionStatus.textContent = `Finding organization providers for ${capabilityId}…`;
      try {
        const payload = await requestJson(
          `/api/v1/source/capabilities/${encodeURIComponent(capabilityId)}/providers`
        );
        if (requestSequence !== providerDetectionSequence) return;
        if (node.eto_reference_id !== capabilityId) return;
        node.provider_candidates = payload.candidates.map(
          (candidate) => candidate.provider_id
        );
        if (nodeElement.selected()) {
          const providerInput = document.getElementById("node-providers");
          if (providerInput) {
            providerInput.value = node.provider_candidates.join(", ");
          }
          const currentStatus = document.getElementById("provider-detection-status");
          if (currentStatus) {
            const preview = payload.candidates.slice(0, 4).map(
              (candidate) => `${candidate.provider_id} — ${candidate.provider_name}`
            );
            const remainder = payload.count - preview.length;
            currentStatus.textContent = payload.count
              ? `Detected ${payload.count}: ${preview.join("; ")}${remainder > 0 ? `; +${remainder} more` : ""}. Editable; ordered by provider ID, not rank.`
              : `No organization providers are recorded for ${capabilityId}. You may enter a provider manually.`;
          }
        }
        markChanged();
      } catch (error) {
        const currentStatus = document.getElementById("provider-detection-status");
        if (currentStatus) {
          currentStatus.textContent = `Provider detection failed: ${error.message}`;
        }
      }
    }

    function basicCheck() {
      const current = buildRecipe();
      if (!/^[a-z][a-z0-9-]{0,62}$/.test(current.product_id)) {
        throw new Error("Product ID must use lowercase letters, numbers, and hyphens.");
      }
      if (!current.product_name) throw new Error("Product name is required.");
      return current;
    }

    async function saveDraft() {
      const current = basicCheck();
      const payload = await requestJson(
        `/api/v1/product-configs/${encodeURIComponent(current.product_id)}/draft`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json", "X-FabTrace-Editor": "1" },
          body: JSON.stringify(current),
        }
      );
      recipe = payload.recipe;
      isNew = false;
      productIdInput.readOnly = true;
      window.history.replaceState({}, "", `/products/${recipe.product_id}/edit`);
      statusElement.textContent = "draft saved";
      statusElement.className = "document-status status-draft";
      reportAction("Draft saved locally.", "success");
      updatePreviews();
      return recipe;
    }

    function renderValidation(result) {
      const panel = document.getElementById("validation-panel");
      if (result.valid) {
        panel.className = "panel validation-panel valid";
        panel.innerHTML = "<div><p class='eyebrow'>Validation</p><h2>Ready to publish</h2></div><p>One output, valid references and units, no cycles, and no disconnected nodes.</p>";
        return;
      }
      panel.className = "panel validation-panel invalid";
      panel.innerHTML = `<div><p class="eyebrow">Validation</p><h2>${result.issues.length} issue${result.issues.length === 1 ? "" : "s"}</h2></div><ul>${result.issues.map((issue) => `<li><strong>${escapeHtml(issue.code)}</strong> ${escapeHtml(issue.message)} <code>${escapeHtml(issue.path)}</code></li>`).join("")}</ul>`;
    }

    async function validateRecipe() {
      const current = basicCheck();
      const result = await requestJson(
        `/api/v1/product-configs/${encodeURIComponent(current.product_id)}/validate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(current),
        }
      );
      renderValidation(result);
      reportAction(
        result.valid
          ? "Validation passed. This draft is ready to publish."
          : `Validation found ${result.issues.length} blocking issue${result.issues.length === 1 ? "" : "s"}.`,
        result.valid ? "success" : "error"
      );
      return result;
    }

    async function runAction(button, action) {
      button.disabled = true;
      try {
        await action();
      } catch (error) {
        reportAction(error.message, "error");
        renderValidation({
          valid: false,
          issues: [{ code: "request_failed", message: error.message, path: "request" }],
        });
      } finally {
        button.disabled = false;
      }
    }

    const saveButton = document.getElementById("save-product");
    const validateButton = document.getElementById("validate-product");
    const publishButton = document.getElementById("publish-product");
    saveButton.addEventListener("click", () => runAction(saveButton, saveDraft));
    validateButton.addEventListener("click", () => runAction(validateButton, validateRecipe));
    publishButton.addEventListener("click", () => runAction(publishButton, async () => {
      reportAction("Saving and validating before publication…");
      await saveDraft();
      const validation = await validateRecipe();
      if (!validation.valid) {
        statusElement.textContent = "publish blocked";
        statusElement.className = "document-status status-draft";
        document.getElementById("validation-panel").scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
        return;
      }
      reportAction("Publishing immutable version…");
      const payload = await requestJson(
        `/api/v1/product-configs/${encodeURIComponent(recipe.product_id)}/publish`,
        { method: "POST", headers: { "X-FabTrace-Editor": "1" } }
      );
      recipe = payload.recipe;
      statusElement.textContent = `published · v${recipe.product_version}`;
      statusElement.className = "document-status status-published";
      reportAction(`Published v${recipe.product_version}. Opening Plan Order…`, "success");
      updatePreviews();
      window.location.assign(
        `/plans/new?product_id=${encodeURIComponent(recipe.product_id)}&product_version=${recipe.product_version}`
      );
    }));

    if (elements.length && Object.values(recipe.nodes).every((node) => node.position.x === 0 && node.position.y === 0)) {
      cy.layout({ name: "breadthfirst", directed: true, padding: 55 }).run();
    }
    updatePreviews();
  });
})();
