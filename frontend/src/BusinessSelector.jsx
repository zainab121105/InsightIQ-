import React from "react";
import { useAuth } from "./AuthProvider";

export function BusinessSelector() {
  const { businesses, selectedBusinessId, setSelectedBusinessId } = useAuth();
  if (businesses.length < 2) return null;
  return (
    <label className="business-selector">
      <span>Workspace</span>
      <select value={selectedBusinessId || ""} onChange={(event) => setSelectedBusinessId(event.target.value)}>
        <option value="" disabled>Select a business</option>
        {businesses.map((business) => (
          <option key={business.business_id} value={business.business_id}>{business.name}</option>
        ))}
      </select>
    </label>
  );
}
