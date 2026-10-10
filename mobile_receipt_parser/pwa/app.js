const hasDom = typeof document !== "undefined";

const imageInput = hasDom ? document.getElementById("imageInput") : null;
const parseBtn = hasDom ? document.getElementById("parseBtn") : null;
const clearBtn = hasDom ? document.getElementById("clearBtn") : null;
const statusText = hasDom ? document.getElementById("statusText") : null;
const preview = hasDom ? document.getElementById("preview") : null;

const merchantValue = hasDom ? document.getElementById("merchantValue") : null;
const currencyValue = hasDom ? document.getElementById("currencyValue") : null;
const itemCountValue = hasDom ? document.getElementById("itemCountValue") : null;
const subtotalValue = hasDom ? document.getElementById("subtotalValue") : null;
const totalValue = hasDom ? document.getElementById("totalValue") : null;
const itemsBody = hasDom ? document.getElementById("itemsBody") : null;

const resultJson = hasDom ? document.getElementById("resultJson") : null;
const ocrText = hasDom ? document.getElementById("ocrText") : null;

const PRICE_TOKEN_RE = /-?\d+[.,]\d{2}-?/g;
const EXCLUDED_LINE_RE =
  /(visa|mastercard|debit|credit|twint|apple\s*pay|google\s*pay|kart|card|zahlung|payment|transaction|transaktion|terminal|beleg|autoris|kasse|mwst|tva|ust|steuer|tax|change|cash|rueckgeld|dank|thank\s*you|bons?|coupon|punkte|superpunkte|nr\.?|ref\.?|trace|aid|acct|emv|\*{2,})/i;
const TOTAL_LINE_RE = /(\btotal\b|betrag|amount\s*due)/i;
const SUBTOTAL_LINE_RE = /(subtotal|zwischensumme|summe)/i;
const DISCOUNT_HINT_RE = /(rabatt|discount|aktion|coupon|gutschein)/i;

function normalizeLine(line) {
  return line.trim().replace(/\s+/g, " ");
}

function formatCHF(value) {
  const number = Number.isFinite(value) ? value : 0;
  return `CHF ${number.toFixed(2)}`;
}

function parseSignedNumber(token) {
  const normalized = token.replace(",", ".").trim();
  const trailingMinus = normalized.endsWith("-");
  const cleaned = normalized.replace(/-$/, "");
  const value = Number.parseFloat(cleaned);
  if (!Number.isFinite(value)) return null;
  if (trailingMinus) return -Math.abs(value);
  if (cleaned.startsWith("-")) return -Math.abs(value);
  return value;
}

function lineHasMostlySymbolsOrDigits(line) {
  const letters = (line.match(/[A-Za-z\u00C0-\u024F]/g) || []).length;
  const digitsOrSymbols = (line.match(/[0-9\W_]/g) || []).length;
  return letters > 0 ? digitsOrSymbols / letters > 8 : true;
}

function extractMerchant(lines) {
  const top = lines.slice(0, 10);

  for (const line of top) {
    const lower = line.toLowerCase();
    if (lower.includes("coop")) return "Coop";
  }

  for (const line of top) {
    if (!line || /\d/.test(line)) continue;
    if (EXCLUDED_LINE_RE.test(line)) continue;
    if (lineHasMostlySymbolsOrDigits(line)) continue;
    return line;
  }

  return "Unknown merchant";
}

function extractPriceTokens(line) {
  return [...line.matchAll(PRICE_TOKEN_RE)]
    .map((m) => parseSignedNumber(m[0]))
    .filter((n) => Number.isFinite(n));
}

function cleanItemName(rawName) {
  let name = rawName
    .replace(/^[|\[\]{}()_\-–—.,:;]+/, "")
    .replace(/\s+[0-9]+(?:[.,][0-9]+)?\s*(kg|g|ml|l|cl)\b/gi, "")
    .replace(/\b[ALO1]\b/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();

  if (name.length > 80) name = name.slice(0, 80).trim();
  return name;
}

function isLikelyItemLine(line) {
  if (!/[A-Za-z\u00C0-\u024F]/.test(line)) return false;
  if (EXCLUDED_LINE_RE.test(line)) return false;
  if (TOTAL_LINE_RE.test(line) || SUBTOTAL_LINE_RE.test(line)) return false;
  return true;
}

function parseItemLine(line) {
  if (!isLikelyItemLine(line)) return null;

  const priceTokens = extractPriceTokens(line);
  if (!priceTokens.length) return null;

  const rawLastTokenMatch = line.match(/-?\d+[.,]\d{2}-?\s*$/);
  const endPrice = rawLastTokenMatch ? parseSignedNumber(rawLastTokenMatch[0]) : null;
  let total = Number.isFinite(endPrice) ? endPrice : priceTokens[priceTokens.length - 1];

  const hasDiscountHint = DISCOUNT_HINT_RE.test(line);
  if (hasDiscountHint && total > 0) {
    total = -Math.abs(total);
  }

  const withoutTrailingPrice = line.replace(/-?\d+[.,]\d{2}-?\s*$/, "").trim();
  const withoutCurrency = withoutTrailingPrice.replace(/\bCHF\b/gi, "").trim();
  let quantity = 1;
  let name = withoutCurrency;

  const qtyPrefix = withoutCurrency.match(/^(\d+)\s*[xX]\s+/);
  if (qtyPrefix) {
    quantity = Number.parseInt(qtyPrefix[1], 10) || 1;
    name = withoutCurrency.slice(qtyPrefix[0].length).trim();
  }

  name = cleanItemName(name);
  if (!name || name.length < 2) return null;

  return {
    name,
    quantity,
    unit_price: Number((total / quantity).toFixed(2)),
    total: Number(total.toFixed(2)),
  };
}

function parseReceiptText(text) {
  const lines = text
    .split("\n")
    .map(normalizeLine)
    .filter(Boolean);

  const merchant = extractMerchant(lines);
  const items = [];
  let subtotal = null;
  let total = null;

  for (const line of lines) {
    if (TOTAL_LINE_RE.test(line)) {
      const prices = extractPriceTokens(line);
      if (prices.length) {
        total = Number(prices[prices.length - 1].toFixed(2));
      }
      continue;
    }

    if (SUBTOTAL_LINE_RE.test(line)) {
      const prices = extractPriceTokens(line);
      if (prices.length) {
        subtotal = Number(prices[prices.length - 1].toFixed(2));
      }
      continue;
    }

    const item = parseItemLine(line);
    if (item) items.push(item);
  }

  if (subtotal == null && items.length) {
    subtotal = Number(items.reduce((sum, item) => sum + item.total, 0).toFixed(2));
  }

  if (total == null) {
    const explicitTotalLine = lines.find((line) => /\btotal\b/i.test(line));
    if (explicitTotalLine) {
      const prices = extractPriceTokens(explicitTotalLine);
      if (prices.length) total = Number(prices[prices.length - 1].toFixed(2));
    }
  }

  if (total == null) total = subtotal ?? 0;

  return {
    merchant,
    currency: "CHF",
    items,
    subtotal: Number((subtotal ?? 0).toFixed(2)),
    total: Number((total ?? 0).toFixed(2)),
  };
}

function setStatus(value) {
  if (statusText) statusText.textContent = value;
}

function renderItems(items) {
  if (!itemsBody) return;

  if (!items.length) {
    itemsBody.innerHTML = '<tr class="empty-row"><td colspan="4">No items found in this receipt.</td></tr>';
    return;
  }

  const rows = items
    .map(
      (item) =>
        `<tr><td>${item.name}</td><td>${item.quantity}</td><td>${formatCHF(item.unit_price)}</td><td>${formatCHF(
          item.total
        )}</td></tr>`
    )
    .join("");

  itemsBody.innerHTML = rows;
}

function renderParsed(parsed, rawText) {
  if (merchantValue) merchantValue.textContent = parsed.merchant || "-";
  if (currencyValue) currencyValue.textContent = parsed.currency || "CHF";
  if (itemCountValue) itemCountValue.textContent = String(parsed.items?.length || 0);
  if (subtotalValue) subtotalValue.textContent = formatCHF(parsed.subtotal);
  if (totalValue) totalValue.textContent = formatCHF(parsed.total);

  renderItems(parsed.items || []);

  if (resultJson) resultJson.textContent = JSON.stringify(parsed, null, 2);
  if (ocrText) ocrText.textContent = rawText && rawText.trim() ? rawText : "(No OCR text captured)";
}

function resetView() {
  if (merchantValue) merchantValue.textContent = "-";
  if (currencyValue) currencyValue.textContent = "CHF";
  if (itemCountValue) itemCountValue.textContent = "0";
  if (subtotalValue) subtotalValue.textContent = "CHF 0.00";
  if (totalValue) totalValue.textContent = "CHF 0.00";
  if (resultJson) resultJson.textContent = "{}";
  if (ocrText) ocrText.textContent = "(OCR text appears here after parsing)";
  renderItems([]);
}

if (hasDom && parseBtn && clearBtn && imageInput && preview) {
  parseBtn.addEventListener("click", async () => {
    const file = imageInput.files?.[0];
    if (!file) {
      setStatus("Choose a receipt image first.");
      return;
    }

    setStatus("Running OCR on-device...");
    parseBtn.disabled = true;

    try {
      const objectUrl = URL.createObjectURL(file);
      preview.src = objectUrl;
      preview.style.display = "block";

      const result = await Tesseract.recognize(file, "eng", {
        logger: (m) => {
          if (m.status && typeof m.progress === "number") {
            setStatus(`${m.status} ${Math.round(m.progress * 100)}%`);
          }
        },
      });

      const rawText = result.data.text || "";
      const parsed = parseReceiptText(rawText);
      renderParsed(parsed, rawText);
      setStatus("Done.");
    } catch (error) {
      console.error(error);
      setStatus("Failed to parse this image.");
    } finally {
      parseBtn.disabled = false;
    }
  });

  clearBtn.addEventListener("click", () => {
    imageInput.value = "";
    preview.removeAttribute("src");
    preview.style.display = "none";
    resetView();
    setStatus("Ready.");
  });

  resetView();

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("./sw.js").catch((error) => {
        console.error("SW registration failed:", error);
      });
    });
  }
}

if (typeof window !== "undefined") {
  window.parseReceiptText = parseReceiptText;
}
