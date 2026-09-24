const DAILY_INTAKE_STORAGE_KEY = "food_inquiry_system.daily_intake";

// 營養累加清單保存至 localStorage；營養計算不由 Store 處理。
function createDailyIntakeStore({ storage, storageKey = DAILY_INTAKE_STORAGE_KEY } = {}) {
  const quantities = new Map();
  let storageBackend = storage;
  if (storageBackend === undefined) {
    try {
      storageBackend = globalThis.localStorage;
    } catch {
      // 瀏覽器禁止存取儲存空間時，仍可使用記憶體清單。
      storageBackend = null;
    }
  }

  function normalizeProductId(productId) {
    if (typeof productId !== "string") return "";
    const id = productId.trim();
    return ["undefined", "null", "nan"].includes(id.toLowerCase()) ? "" : id;
  }

  function snapshotItems() {
    return Array.from(quantities, ([productId, quantity]) => ({ productId, quantity }));
  }

  function save() {
    try {
      storageBackend?.setItem(storageKey, JSON.stringify({
        items: snapshotItems(),
      }));
    } catch {
      // 配額不足或禁止寫入時不影響既有 UI 與記憶體清單。
    }
  }

  function restore() {
    quantities.clear();
    try {
      const saved = storageBackend?.getItem(storageKey);
      if (saved === null || saved === undefined) {
        save();
        return;
      }
      const payload = JSON.parse(saved);
      if (payload === null || typeof payload !== "object" || Array.isArray(payload) ||
          !Array.isArray(payload.items)) {
        save();
        return;
      }
      payload.items.forEach((item) => {
        if (item === null || typeof item !== "object" || Array.isArray(item)) return;
        const id = normalizeProductId(item.productId);
        if (!id || !Number.isFinite(item.quantity) || item.quantity <= 0) return;
        // 還原時若有重複 id，保留第一筆有效資料。
        if (!quantities.has(id)) quantities.set(id, item.quantity);
      });
      // 回存經過驗證的標準格式，移除無效或重複項目。
      save();
    } catch {
      // 損壞 JSON 或讀取失敗時安全回復空清單。
      quantities.clear();
      save();
    }
  }

  // 操作成功回傳 true；輸入無效或找不到項目時回傳 false，不改動清單。
  function addProduct(productId) {
    const id = normalizeProductId(productId);
    if (!id) return false;
    const currentQuantity = quantities.get(id) ?? 0;
    const nextQuantity = currentQuantity + 1;
    // 避免溢位或數字精度不足導致「加入」後數量未增加。
    if (!Number.isFinite(nextQuantity) || nextQuantity <= currentQuantity) return false;
    quantities.set(id, nextQuantity);
    save();
    return true;
  }

  function removeProduct(productId) {
    const id = normalizeProductId(productId);
    if (!id || !quantities.delete(id)) return false;
    save();
    return true;
  }

  function updateQuantity(productId, quantity) {
    const id = normalizeProductId(productId);
    if (!id || !quantities.has(id) || !Number.isFinite(quantity) || quantity <= 0) return false;
    quantities.set(id, quantity);
    save();
    return true;
  }

  // 回傳新的陣列與物件，呼叫端修改結果不會改動內部清單。
  function getItems() {
    return snapshotItems();
  }

  function clear() {
    quantities.clear();
    save();
  }

  restore();
  return Object.freeze({ addProduct, removeProduct, updateQuantity, getItems, clear });
}

const dailyIntakeStore = createDailyIntakeStore();
