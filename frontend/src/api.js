const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (body.detail && typeof body.detail.message === "string") {
        detail = body.detail.retry_after_seconds != null
          ? `${body.detail.message} You can retry in about ${body.detail.retry_after_seconds} seconds.`
          : body.detail.message;
      }
    } catch {
      // Keep the useful HTTP status when the server does not return JSON.
    }
    throw new Error(detail);
  }

  return response.json();
}

export const getSummary = () => request("/analytics/summary");
export const getProducts = () => request("/analytics/products");
export const getRegions = () => request("/analytics/regions");
export const getMonthly = () => request("/analytics/monthly");

export const uploadSalesPreview = async (file) => {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch(`${API_BASE_URL}/sales/upload-preview`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let detail = `Upload preview failed with status ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // fallback to generic status message
    }
    throw new Error(detail);
  }

  return response.json();
};

export const confirmSalesImport = (previewId) =>
  request("/sales/import-confirm", {
    method: "POST",
    body: JSON.stringify({ preview_id: previewId }),
  });

export const askAssistant = (question, conversationId, previousMessages) =>
  request("/chat", {
    method: "POST",
    body: JSON.stringify({
      question,
      conversation_id: conversationId,
      messages: previousMessages,
    }),
  });