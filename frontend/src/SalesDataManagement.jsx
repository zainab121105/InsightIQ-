import React, { useRef, useState } from "react";
import { confirmSalesImport, uploadSalesPreview } from "./api";

const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024; // 10 MB

const CSV_TEMPLATE_CONTENT = `Transaction_Id,Order_date,Product,Category,Region,Sales_channel,Quantity,Unit_price,Discount_amount,Status
TXN1001,2026-07-01,Wireless Mouse,Accessories,South,Retail,15,650,0,Completed
TXN1002,05-07-2026,Office Chair,Furniture,North,Online,4,7800,500,Completed
`;

function money(value) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(Number(value || 0));
}

function number(value) {
  return new Intl.NumberFormat("en-IN").format(Number(value || 0));
}

function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

export function SalesDataManagement({ onImportSuccess, showToast }) {
  const [file, setFile] = useState(null);
  const [dragActive, setDragActive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState("");
  const [previewData, setPreviewData] = useState(null);
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [importResult, setImportResult] = useState(null);

  const fileInputRef = useRef(null);

  const handleDownloadTemplate = () => {
    const blob = new Blob([CSV_TEMPLATE_CONTENT], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "insightiq_sales_template.csv";
    a.click();
    URL.revokeObjectURL(url);
    showToast("Sales template downloaded.");
  };

  const processFileSelection = async (selectedFile) => {
    if (!selectedFile) return;

    setError("");
    setImportResult(null);

    const name = selectedFile.name.toLowerCase();
    if (!name.endsWith(".csv") && !name.endsWith(".xlsx")) {
      setError("Unsupported file format. Please upload a .csv or .xlsx file.");
      return;
    }

    if (selectedFile.size > MAX_FILE_SIZE_BYTES) {
      setError("File exceeds maximum allowed size of 10 MB.");
      return;
    }

    setFile(selectedFile);
    setLoading(true);

    try {
      const data = await uploadSalesPreview(selectedFile);
      setPreviewData(data);
      showToast("File analyzed and validated.");
    } catch (err) {
      setError(err.message || "Failed to analyze uploaded file.");
      setPreviewData(null);
    } finally {
      setLoading(false);
    }
  };

  const handleDragOver = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(true);
  };

  const handleDragLeave = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (e.dataTransfer?.files && e.dataTransfer.files[0]) {
      processFileSelection(e.dataTransfer.files[0]);
    }
  };

  const handleFileChange = (e) => {
    if (e.target?.files && e.target.files[0]) {
      processFileSelection(e.target.files[0]);
    }
  };

  const handleConfirmImport = async () => {
    if (!previewData?.preview_id) return;

    setImporting(true);
    setError("");

    try {
      const result = await confirmSalesImport(previewData.preview_id);
      setImportResult(result);
      setShowConfirmModal(false);
      setPreviewData(null);
      setFile(null);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      showToast(result.message || "Sales transactions imported successfully!");

      // Trigger automatic dashboard analytics refresh without page reload
      if (typeof onImportSuccess === "function") {
        await onImportSuccess();
      }
    } catch (err) {
      setError(err.message || "Import confirmation failed.");
      setShowConfirmModal(false);
    } finally {
      setImporting(false);
    }
  };

  const summary = previewData?.summary;
  const validRecords = previewData?.valid_records_preview || [];
  const rejectedRecords = previewData?.rejected_records || [];

  return (
    <section className="page-shell">
      <div className="page-heading">
        <div>
          <span className="eyebrow">DATA MANAGEMENT</span>
          <h1>Sales data import</h1>
          <p>
            Upload CSV or Excel spreadsheets to ingest new transactions into MySQL.
          </p>
        </div>
        <button className="outline-button" onClick={handleDownloadTemplate}>
          ↓ Download template (.csv)
        </button>
      </div>

      {/* Success banner if an import just completed */}
      {importResult && (
        <div className="surface success-banner">
          <div className="success-badge">✓ Imported Successfully</div>
          <h2>{importResult.message}</h2>
          <div className="import-kpis">
            <div>
              <span>Transactions added</span>
              <b>{number(importResult.imported_count)}</b>
            </div>
            <div>
              <span>New revenue recorded</span>
              <b>{money(importResult.imported_revenue)}</b>
            </div>
            <div>
              <span>Quantity sold</span>
              <b>{number(importResult.imported_quantity)} units</b>
            </div>
          </div>
          <p className="success-note">
            ✦ All dashboard analytics and overview leaderboards have been refreshed with this data.
          </p>
        </div>
      )}

      {/* Error state */}
      {error && (
        <div className="error-box">
          <span>{error}</span>
          <button onClick={() => setError("")}>Dismiss</button>
        </div>
      )}

      {/* Dropzone Upload Section */}
      <div
        className={`upload-dropzone surface ${dragActive ? "drag-active" : ""}`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <div className="dropzone-icon">⊞</div>
        <h3>Drop your sales file here, or browse</h3>
        <p>Supports .csv and .xlsx spreadsheets up to 10 MB</p>

        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,.xlsx"
          style={{ display: "none" }}
          onChange={handleFileChange}
        />

        <div className="dropzone-actions">
          <button
            type="button"
            className="violet-button"
            onClick={() => fileInputRef.current?.click()}
            disabled={loading || importing}
          >
            {loading ? "Validating file..." : "Select sales file"}
          </button>
        </div>

        {file && (
          <div className="selected-file-chip">
            <span>📄 {file.name}</span>
            <small>({formatBytes(file.size)})</small>
          </div>
        )}

        <div className="required-fields-guide">
          <b>Required 10 Columns:</b>
          <code>Transaction_Id</code>, <code>Order_date</code>, <code>Product</code>,{" "}
          <code>Category</code>, <code>Region</code>, <code>Sales_channel</code>,{" "}
          <code>Quantity</code>, <code>Unit_price</code>, <code>Discount_amount</code>,{" "}
          <code>Status</code>
          <small>
            (Server automatically calculates revenue and order month. Only 'Completed' transactions with positive quantity and price are accepted.)
          </small>
        </div>
      </div>

      {/* Loading state indicator */}
      {loading && (
        <div className="state-box">
          <span className="loader" /> Analyzing file and validating against MySQL...
        </div>
      )}

      {/* Preview Section */}
      {previewData && !loading && (
        <div className="preview-container">
          {/* Ingestion KPI Summary Grid */}
          <div className="surface preview-summary-card">
            <div className="section-head">
              <div>
                <h2>Ingestion analysis</h2>
                <span>Validation summary and duplicate check</span>
              </div>
              <button
                className="violet-button"
                disabled={!summary?.valid_rows_count || importing}
                onClick={() => setShowConfirmModal(true)}
              >
                {importing ? "Importing..." : `Confirm & import ${summary?.valid_rows_count || 0} records →`}
              </button>
            </div>

            <div className="preview-kpi-grid">
              <div className="preview-kpi">
                <span>Total records</span>
                <b>{number(summary?.total_rows)}</b>
                <small>Rows in sheet</small>
              </div>
              <div className="preview-kpi highlight-green">
                <span>Valid records</span>
                <b>{number(summary?.valid_rows_count)}</b>
                <small>Eligible for import</small>
              </div>
              <div className="preview-kpi highlight-amber">
                <span>Invalid records</span>
                <b>{number(summary?.invalid_rows_count)}</b>
                <small>Violated rules</small>
              </div>
              <div className="preview-kpi highlight-red">
                <span>DB collisions</span>
                <b>{number(summary?.existing_db_duplicates_count)}</b>
                <small>Already in MySQL</small>
              </div>
              <div className="preview-kpi">
                <span>In-file duplicates</span>
                <b>{number(summary?.in_file_duplicates_count)}</b>
                <small>Repeated IDs</small>
              </div>
              <div className="preview-kpi highlight-violet">
                <span>Projected revenue</span>
                <b>{money(summary?.projected_revenue)}</b>
                <small>Server calculated</small>
              </div>
            </div>
          </div>

          {/* Valid Records Preview Table */}
          {validRecords.length > 0 && (
            <div className="surface table-surface">
              <div className="section-head">
                <div>
                  <h2>Valid transactions preview</h2>
                  <span>First {validRecords.length} records ready for insertion</span>
                </div>
              </div>
              <div className="responsive-table-wrap">
                <table className="clean-table">
                  <thead>
                    <tr>
                      <th>Transaction ID</th>
                      <th>Date</th>
                      <th>Product</th>
                      <th>Category</th>
                      <th>Region</th>
                      <th>Channel</th>
                      <th>Qty</th>
                      <th>Unit Price</th>
                      <th>Discount</th>
                      <th>Calculated Revenue</th>
                    </tr>
                  </thead>
                  <tbody>
                    {validRecords.map((row) => (
                      <tr key={row.transaction_id}>
                        <td><code>{row.transaction_id}</code></td>
                        <td>{row.order_date}</td>
                        <td><b>{row.product}</b></td>
                        <td>{row.category}</td>
                        <td>{row.region}</td>
                        <td>{row.sales_channel}</td>
                        <td>{row.quantity}</td>
                        <td>{money(row.unit_price)}</td>
                        <td>{money(row.discount_amount)}</td>
                        <td><b className="green-text">{money(row.revenue)}</b></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Rejected Records Table */}
          {rejectedRecords.length > 0 && (
            <div className="surface table-surface rejected-surface">
              <div className="section-head">
                <div>
                  <h2>Rejected records ({rejectedRecords.length})</h2>
                  <span>These records will not be imported into the database</span>
                </div>
              </div>
              <div className="responsive-table-wrap">
                <table className="clean-table">
                  <thead>
                    <tr>
                      <th>Row #</th>
                      <th>Transaction ID</th>
                      <th>Rejection Reasons</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rejectedRecords.map((rej, idx) => (
                      <tr key={`${rej.transaction_id}-${idx}`}>
                        <td>{rej.row_number}</td>
                        <td><code>{rej.transaction_id}</code></td>
                        <td>
                          <div className="tag-stack">
                            {rej.rejection_reasons.map((r, rIdx) => (
                              <span key={rIdx} className="rejection-tag">
                                {r}
                              </span>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Confirmation Modal */}
      {showConfirmModal && (
        <div className="modal-backdrop" onClick={() => !importing && setShowConfirmModal(false)}>
          <div className="modal-box surface" onClick={(e) => e.stopPropagation()}>
            <h3>Confirm sales import</h3>
            <p>
              Are you sure you want to import <b>{summary?.valid_rows_count} valid transactions</b> into MySQL?
            </p>
            <div className="modal-impact-summary">
              <div>
                <span>Projected revenue addition</span>
                <b>{money(summary?.projected_revenue)}</b>
              </div>
              <div>
                <span>Total units added</span>
                <b>{number(summary?.projected_quantity)} units</b>
              </div>
            </div>
            <p className="modal-warning">
              * This operation uses an atomic database transaction. Any existing database records will remain unchanged.
            </p>
            <div className="modal-actions">
              <button
                type="button"
                className="outline-button"
                onClick={() => setShowConfirmModal(false)}
                disabled={importing}
              >
                Cancel
              </button>
              <button
                type="button"
                className="violet-button"
                onClick={handleConfirmImport}
                disabled={importing}
              >
                {importing ? "Importing records..." : "Yes, import to database"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
