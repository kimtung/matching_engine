const BACKEND_HTTP_URL = window.localStorage.getItem("matching-engine-backend") || "http://127.0.0.1:8000";
const BACKEND_WS_URL = BACKEND_HTTP_URL.replace(/^http/, "ws") + "/ws";

const orderForm = document.getElementById("order-form");
const orderTypeInput = document.getElementById("order-type");
const priceField = document.getElementById("price-field");
const priceLabel = document.getElementById("price-label");
const priceInput = document.getElementById("price");
const stopPriceField = document.getElementById("stop-price-field");
const stopPriceInput = document.getElementById("stop-price");
const stopHint = document.getElementById("stop-hint");
const lastPricePill = document.getElementById("last-price");
const resetBtn = document.getElementById("reset-btn");
const flash = document.getElementById("flash");
const socketStatus = document.getElementById("socket-status");
const backendTarget = document.getElementById("backend-target");
let socket;

backendTarget.textContent = `Backend: ${BACKEND_HTTP_URL}`;

function showMessage(message, kind = "info") {
  flash.textContent = message;
  flash.className = `flash ${kind}`;
  setTimeout(() => {
    flash.className = "flash hidden";
  }, 2400);
}

function formatPrice(value) {
  if (value === null || value === undefined) {
    return "-";
  }
  return Number(value).toFixed(2);
}

function formatTime(value) {
  return new Date(Number(value)).toLocaleTimeString();
}

function renderRows(targetId, rows, renderRow, emptyCols = 6) {
  const target = document.getElementById(targetId);
  target.innerHTML = rows.length ? rows.map(renderRow).join("") : `<tr><td colspan="${emptyCols}" class="empty">No data</td></tr>`;
}

function renderBook(state) {
  const buys = state.book.buys;
  const sells = state.book.sells;
  const trades = state.book.trades;
  const stops = state.book.stops || [];
  const lastPrice = state.book.last_price;
  const activeOrders = state.active_orders;

  document.getElementById("buy-depth").textContent = `${buys.length} levels`;
  document.getElementById("sell-depth").textContent = `${sells.length} levels`;
  document.getElementById("trade-count").textContent = `${trades.length} trades`;
  document.getElementById("order-count").textContent = `${activeOrders.length} orders`;
  document.getElementById("stop-count").textContent = `${stops.length} stops`;
  lastPricePill.textContent = `Last: ${lastPrice !== null && lastPrice !== undefined ? formatPrice(lastPrice) : "-"}`;

  renderRows("buys-body", buys, (order) => `
    <tr>
      <td>${order.order_id}</td>
      <td>${formatPrice(order.price)}</td>
      <td>${order.remaining}</td>
      <td>${formatTime(order.timestamp)}</td>
    </tr>
  `, 4);

  renderRows("sells-body", sells, (order) => `
    <tr>
      <td>${order.order_id}</td>
      <td>${formatPrice(order.price)}</td>
      <td>${order.remaining}</td>
      <td>${formatTime(order.timestamp)}</td>
    </tr>
  `, 4);

  renderRows("trades-body", trades, (trade) => `
    <tr>
      <td>${trade.buy_order_id}</td>
      <td>${trade.sell_order_id}</td>
      <td>${formatPrice(trade.price)}</td>
      <td>${trade.quantity}</td>
      <td>${trade.aggressor_order_id}</td>
      <td>${formatTime(trade.timestamp)}</td>
    </tr>
  `, 6);

  renderRows("orders-body", activeOrders, (order) => `
    <tr>
      <td>${order.order_id}</td>
      <td>${order.side}</td>
      <td>${order.order_type}</td>
      <td>${formatPrice(order.price)}</td>
      <td>${order.remaining}</td>
      <td><button class="danger small" data-cancel="${order.order_id}">Cancel</button></td>
    </tr>
  `, 6);

  renderRows("stops-body", stops, (order) => `
    <tr>
      <td>${order.order_id}</td>
      <td>${order.side}</td>
      <td>${order.order_type}</td>
      <td>${formatPrice(order.stop_price)}</td>
      <td>${formatPrice(order.price)}</td>
      <td>${order.remaining}</td>
      <td>${formatTime(order.timestamp)}</td>
      <td><button class="danger small" data-cancel="${order.order_id}">Cancel</button></td>
    </tr>
  `, 8);
}

async function fetchState() {
  const response = await fetch(`${BACKEND_HTTP_URL}/api/state`);
  const state = await response.json();
  renderBook(state);
}

function setSocketStatus(text, statusClass) {
  socketStatus.textContent = text;
  socketStatus.className = `pill socket ${statusClass}`;
}

function connectSocket() {
  socket = new WebSocket(BACKEND_WS_URL);

  socket.addEventListener("open", () => {
    setSocketStatus("Socket connected", "online");
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    renderBook(message.state);
    if (message.event === "order_placed" && message.trades.length > 0) {
      showMessage(`Trade matched: ${message.trades.length}`, "success");
    }
  });

  socket.addEventListener("close", () => {
    setSocketStatus("Socket reconnecting", "offline");
    window.setTimeout(connectSocket, 1500);
  });

  socket.addEventListener("error", () => {
    setSocketStatus("Socket error", "offline");
    socket.close();
  });
}

function updateFormFields() {
  const type = orderTypeInput.value;
  const needsPrice = type === "LIMIT" || type === "STOP_LIMIT";
  const needsStop = type === "STOP_MARKET" || type === "STOP_LIMIT";

  priceField.style.display = needsPrice ? "block" : "none";
  priceInput.toggleAttribute("required", needsPrice);
  priceLabel.textContent = type === "STOP_LIMIT" ? "Limit Price" : "Price";

  stopPriceField.style.display = needsStop ? "block" : "none";
  stopPriceInput.toggleAttribute("required", needsStop);
  stopHint.style.display = needsStop ? "block" : "none";
}

orderTypeInput.addEventListener("change", updateFormFields);

orderForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(orderForm);
  const payload = Object.fromEntries(formData.entries());
  const type = payload.order_type;

  if (type === "LIMIT" || type === "STOP_LIMIT") {
    payload.price = Number(payload.price);
  } else {
    delete payload.price;
  }
  if (type === "STOP_MARKET" || type === "STOP_LIMIT") {
    payload.stop_price = Number(payload.stop_price);
  } else {
    delete payload.stop_price;
  }
  payload.quantity = Number(payload.quantity);

  const response = await fetch(`${BACKEND_HTTP_URL}/api/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    let err = "Order rejected";
    try {
      const data = await response.json();
      if (data && data.error) err = data.error;
    } catch (_) {}
    showMessage(err, "error");
    return;
  }
  showMessage(`Placed ${type} ${payload.side} order`, "success");
  orderForm.reset();
  updateFormFields();
});

async function handleCancelClick(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  const orderId = target.dataset.cancel;
  if (!orderId) {
    return;
  }

  const response = await fetch(`${BACKEND_HTTP_URL}/api/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order_id: orderId }),
  });
  const data = await response.json();
  showMessage(data.ok ? `Cancelled ${orderId}` : `${orderId} not found`, data.ok ? "success" : "error");
}

document.getElementById("orders-body").addEventListener("click", handleCancelClick);
document.getElementById("stops-body").addEventListener("click", handleCancelClick);

resetBtn.addEventListener("click", async () => {
  const response = await fetch(`${BACKEND_HTTP_URL}/api/reset`, { method: "POST" });
  await response.json();
  showMessage("Order book reset", "success");
});

updateFormFields();
fetchState();
connectSocket();
