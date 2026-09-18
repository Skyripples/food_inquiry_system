// 今日食品清單僅存在記憶體，不處理日期切換、持久化或營養計算；營養加總由獨立模組處理。
function createDailyIntakeStore() {
  const quantities = new Map();

  function normalizeProductId(productId) {
    return typeof productId === "string" ? productId.trim() : "";
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
    return true;
  }

  function removeProduct(productId) {
    const id = normalizeProductId(productId);
    return id ? quantities.delete(id) : false;
  }

  function updateQuantity(productId, quantity) {
    const id = normalizeProductId(productId);
    if (!id || !quantities.has(id) || !Number.isFinite(quantity) || quantity <= 0) return false;
    quantities.set(id, quantity);
    return true;
  }

  // 回傳新的陣列與物件，呼叫端修改結果不會改動內部清單。
  function getItems() {
    return Array.from(quantities, ([productId, quantity]) => ({ productId, quantity }));
  }

  function clear() {
    quantities.clear();
  }

  return Object.freeze({ addProduct, removeProduct, updateQuantity, getItems, clear });
}

const dailyIntakeStore = createDailyIntakeStore();
