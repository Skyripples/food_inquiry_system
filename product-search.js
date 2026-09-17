function normalizeSearchText(value) {
  return typeof value === "string" || (typeof value === "number" && Number.isFinite(value))
    ? String(value).trim().toLocaleLowerCase("zh-Hant")
    : "";
}

function findProducts(products, query) {
  const normalizedQuery = normalizeSearchText(query);
  if (!normalizedQuery) return [];
  return products.filter((product) =>
    [product.name, product.brand, product.barcode].some((field) =>
      normalizeSearchText(field).includes(normalizedQuery),
    ),
  );
}
