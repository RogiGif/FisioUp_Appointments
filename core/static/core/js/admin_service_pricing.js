document.addEventListener("DOMContentLoaded", () => {
  const pricingSelect = document.querySelector('select[name="pricing_mode"]');
  const priceRow = document.querySelector(".form-row.field-price");
  const priceFirstRow = document.querySelector(".form-row.field-price_first");
  const priceFollowRow = document.querySelector(".form-row.field-price_followup");
  const priceInput = priceRow ? priceRow.querySelector('input[name="price"]') : null;
  if (!pricingSelect || !priceRow) return;

  const togglePrice = () => {
    const isFirstFollow = pricingSelect.value === "first_followup";
    priceRow.style.display = isFirstFollow ? "none" : "";
    if (priceFirstRow) priceFirstRow.style.display = isFirstFollow ? "" : "none";
    if (priceFollowRow) priceFollowRow.style.display = isFirstFollow ? "" : "none";
    if (priceInput) {
      priceInput.disabled = isFirstFollow;
      if (isFirstFollow && !priceInput.value) {
        priceInput.value = "0.00";
      }
    }
  };

  togglePrice();
  pricingSelect.addEventListener("change", togglePrice);
});
