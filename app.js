const searchForm = document.querySelector("#search-form");
const pageElements = {
  section: document.querySelector(".results-section"),
  input: document.querySelector("#search-input"),
  button: searchForm.querySelector("button"),
  results: document.querySelector("#product-results"),
  status: document.querySelector("#status-message"),
  count: document.querySelector("#result-count"),
};
let pageState = PAGE_STATES.INITIAL;

function setPageState(state, count = null) {
  pageState = state;
  renderPageState(pageElements, state, count);
}

function showProduct(productId, count) {
  const product = productRepository.getProductById(productId);
  if (!product) {
    setPageState(PAGE_STATES.NOT_FOUND, 0);
    return;
  }
  setPageState(PAGE_STATES.SUCCESS, count);
  renderProductDetail(pageElements.results, product);
}

function handleSearch() {
  if ([PAGE_STATES.LOADING, PAGE_STATES.LOAD_ERROR, PAGE_STATES.NO_DATA].includes(pageState)) return;
  const query = pageElements.input.value;
  if (!query.trim()) {
    setPageState(PAGE_STATES.INITIAL);
    return;
  }
  const matches = productRepository.searchProducts(query);
  if (matches.length === 0) {
    setPageState(PAGE_STATES.NOT_FOUND, 0);
  } else if (matches.length === 1) {
    showProduct(matches[0].id, 1);
  } else {
    setPageState(PAGE_STATES.MULTIPLE, matches.length);
    renderProductList(pageElements.results, matches, (product) => {
      showProduct(product.id, matches.length);
      const heading = pageElements.results.querySelector("h3");
      heading.tabIndex = -1;
      heading.focus();
    });
  }
}

async function initializeProducts() {
  setPageState(PAGE_STATES.LOADING);
  try {
    const { count } = await productRepository.loadProducts();
    setPageState(count ? PAGE_STATES.INITIAL : PAGE_STATES.NO_DATA);
  } catch {
    // 可預期的載入失敗由頁面狀態處理，避免未捕捉例外。
    setPageState(PAGE_STATES.LOAD_ERROR);
  }
}

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  handleSearch();
});

setPageState(PAGE_STATES.INITIAL);
initializeProducts();
