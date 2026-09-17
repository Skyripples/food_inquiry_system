// 換成 API 或其他資料來源時，可替換 loadData，維持 Repository 介面不變。
async function loadProductData() {
  const response = await fetch("data/products.json");
  if (!response.ok) throw new Error("Products request failed");
  return response.json();
}

function createProductRepository({ loadData = loadProductData } = {}) {
  let products = [];
  let productsById = new Map();

  // 成功回傳 { count }；失敗拋出例外，交由 app 的既有錯誤狀態處理。
  async function loadProducts() {
    // 重新載入期間或失敗後，不讓舊資料繼續被搜尋。
    products = [];
    productsById = new Map();
    const data = await loadData();
    if (!Array.isArray(data?.products)) throw new TypeError("Invalid products data");
    products = validateProducts(data.products);
    productsById = new Map(products.map((product) => [product.id, product]));
    return { count: products.length };
  }

  function searchProducts(keyword) {
    return findProducts(products, keyword);
  }

  // 找不到 id 時回傳 null，不拋出例外。
  function getProductById(id) {
    return productsById.get(id) ?? null;
  }

  return Object.freeze({ loadProducts, searchProducts, getProductById });
}

const productRepository = createProductRepository();
