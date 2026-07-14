from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.schemas.common import DocumentType, FieldConfidence, ValidatedDocument, ValidationIssue


class ValidationService:
    TOLERANCE = 1.0

    def validate(self, data: dict[str, Any]) -> ValidatedDocument:
        try:
            doc_type = DocumentType(data.get("document_type", "unknown"))
        except ValueError:
            doc_type = DocumentType.unknown

        issues: list[ValidationIssue] = []

        # Run validations
        if doc_type == DocumentType.cma:
            from app.services.cma_validator import validate_extraction
            # Validate using CMA validator
            validated_cma = validate_extraction(data)
            # Map warnings to ValidationIssue format
            val_summary = validated_cma.get("validation", {})
            for w in val_summary.get("warnings", []):
                issues.append(
                    ValidationIssue(
                        field="cma_data",
                        status=w["check"],
                        message=w["message"],
                        confidence=0.4 if w["severity"] == "high" else 0.6
                    )
                )
            for w in val_summary.get("unit_warnings", []):
                issues.append(
                    ValidationIssue(
                        field="cma_data",
                        status="unit_mismatch",
                        message=w["message"],
                        confidence=0.5
                    )
                )
            for f in val_summary.get("low_confidence_fields", []):
                issues.append(
                    ValidationIssue(
                        field=f"{f['section']}.{f['field']}",
                        status="low_confidence",
                        message=f"Low confidence ({f['confidence']}) for {f['field']} in {f['year']}",
                        confidence=f['confidence']
                    )
                )
        else:
            self._validate_required_fields(doc_type, data, issues)
            self._validate_types(doc_type, data, issues)
            self._validate_amounts(doc_type, data, issues)
            self._detect_duplicates(doc_type, data, issues)

        # Generate confidence scores based on validation issues
        field_confidence = self._score_fields(data, issues)

        return ValidatedDocument(
            document_type=doc_type,
            data=data,
            field_confidence=field_confidence,
            issues=issues,
        )

    def _score_fields(self, data: dict[str, Any], issues: list[ValidationIssue]) -> dict[str, FieldConfidence]:
        scores: dict[str, FieldConfidence] = {}
        for key, value in data.items():
            if isinstance(value, list):
                confidence = 0.9 if value else 0.2
            elif isinstance(value, dict):
                confidence = 0.85 if value else 0.2
            else:
                confidence = 0.95 if value not in (None, "", []) else 0.0
            scores[key] = FieldConfidence(value=value, confidence=confidence)

        # Penalize confidence scores based on issues found
        for issue in issues:
            field = issue.field
            if field in scores:
                scores[field].confidence = min(scores[field].confidence, issue.confidence)

        return scores

    def _validate_required_fields(
        self, doc_type: DocumentType, data: dict[str, Any], issues: list[ValidationIssue]
    ) -> None:
        required = {
            DocumentType.invoice: ["invoice_number", "invoice_date", "vendor_name", "grand_total"],
            DocumentType.receipt: ["merchant_name", "date", "total"],
            DocumentType.bank_statement: ["bank_name", "account_number", "transactions"],
            DocumentType.purchase_order: ["po_number", "po_date", "vendor_name", "grand_total"],
            DocumentType.financial_report: ["report_name", "period"],
        }.get(doc_type, [])

        for field in required:
            val = data.get(field)
            if val in (None, "", []):
                issues.append(
                    ValidationIssue(
                        field=field,
                        status="missing",
                        message=f"Required field '{field}' is missing",
                        confidence=0.0,
                    )
                )

    def _validate_types(self, doc_type: DocumentType, data: dict[str, Any], issues: list[ValidationIssue]) -> None:
        # Field mapping categories
        date_fields = {"invoice_date", "date", "po_date"}
        string_fields = {
            "invoice_number",
            "vendor_name",
            "vendor_gstin",
            "customer_name",
            "customer_gstin",
            "merchant_name",
            "receipt_number",
            "bank_name",
            "account_number",
            "statement_period",
            "po_number",
            "buyer_name",
            "report_name",
            "period",
        }
        float_fields = {
            "subtotal",
            "cgst",
            "sgst",
            "igst",
            "total_tax",
            "grand_total",
            "tax",
            "total",
        }

        # Check document-level fields
        for key, val in data.items():
            if val in (None, ""):
                continue

            if key in date_fields:
                if not self._is_valid_date(val):
                    issues.append(
                        ValidationIssue(
                            field=key,
                            status="invalid_type",
                            message=f"Field '{key}' has an invalid date format: '{val}'",
                            confidence=0.3,
                        )
                    )
            elif key in float_fields:
                if not self._is_valid_float(val):
                    issues.append(
                        ValidationIssue(
                            field=key,
                            status="invalid_type",
                            message=f"Field '{key}' must be a float: '{val}'",
                            confidence=0.3,
                        )
                    )
            elif key in string_fields:
                if not isinstance(val, str):
                    issues.append(
                        ValidationIssue(
                            field=key,
                            status="invalid_type",
                            message=f"Field '{key}' must be a string",
                            confidence=0.3,
                        )
                    )
            elif key == "currency":
                if not self._is_valid_currency(val):
                    issues.append(
                        ValidationIssue(
                            field=key,
                            status="invalid_type",
                            message=f"Field 'currency' is invalid: '{val}'",
                            confidence=0.4,
                        )
                    )

        # Check line items nested fields
        if doc_type in (DocumentType.invoice, DocumentType.purchase_order):
            for i, item in enumerate(data.get("line_items") or []):
                for k, v in item.items():
                    if v in (None, ""):
                        continue
                    if k == "description" and not isinstance(v, str):
                        issues.append(
                            ValidationIssue(
                                field="line_items",
                                status="invalid_type",
                                message=f"Line item {i+1} description must be a string",
                                confidence=0.5,
                            )
                        )
                    elif k in ("quantity", "unit_price", "amount") and not self._is_valid_float(v):
                        issues.append(
                            ValidationIssue(
                                field="line_items",
                                status="invalid_type",
                                message=f"Line item {i+1} field '{k}' must be a float: '{v}'",
                                confidence=0.5,
                            )
                        )

        # Check bank transactions nested fields
        elif doc_type == DocumentType.bank_statement:
            for i, tx in enumerate(data.get("transactions") or []):
                for k, v in tx.items():
                    if v in (None, ""):
                        continue
                    if k == "date" and not self._is_valid_date(v):
                        issues.append(
                            ValidationIssue(
                                field="transactions",
                                status="invalid_type",
                                message=f"Transaction {i+1} date is invalid: '{v}'",
                                confidence=0.4,
                            )
                        )
                    elif k in ("debit", "credit", "balance") and not self._is_valid_float(v):
                        issues.append(
                            ValidationIssue(
                                field="transactions",
                                status="invalid_type",
                                message=f"Transaction {i+1} field '{k}' must be a float: '{v}'",
                                confidence=0.4,
                            )
                        )
                    elif k == "description" and not isinstance(v, str):
                        issues.append(
                            ValidationIssue(
                                field="transactions",
                                status="invalid_type",
                                message=f"Transaction {i+1} description must be a string",
                                confidence=0.5,
                            )
                        )

    def _validate_amounts(self, doc_type: DocumentType, data: dict[str, Any], issues: list[ValidationIssue]) -> None:
        if doc_type == DocumentType.invoice:
            subtotal = self._num(data.get("subtotal"))
            taxes = sum(self._num(data.get(k)) for k in ("cgst", "sgst", "igst"))
            total_tax = self._num(data.get("total_tax"))
            grand_total = self._num(data.get("grand_total"))
            tax_value = total_tax if total_tax else taxes

            if subtotal and grand_total and not math.isclose(subtotal + tax_value, grand_total, abs_tol=self.TOLERANCE):
                issues.append(
                    ValidationIssue(
                        field="grand_total",
                        status="amount_mismatch",
                        message=f"Subtotal ({subtotal}) + tax ({tax_value}) does not match grand_total ({grand_total}) within tolerance",
                        confidence=0.45,
                    )
                )

        elif doc_type == DocumentType.receipt:
            subtotal = self._num(data.get("subtotal"))
            tax = self._num(data.get("tax"))
            total = self._num(data.get("total"))

            if subtotal and total and not math.isclose(subtotal + tax, total, abs_tol=self.TOLERANCE):
                issues.append(
                    ValidationIssue(
                        field="total",
                        status="amount_mismatch",
                        message=f"Subtotal ({subtotal}) + tax ({tax}) does not match total ({total}) within tolerance",
                        confidence=0.45,
                    )
                )

        elif doc_type == DocumentType.purchase_order:
            subtotal = self._num(data.get("subtotal"))
            tax = self._num(data.get("tax"))
            grand_total = self._num(data.get("grand_total"))

            if subtotal and grand_total and not math.isclose(subtotal + tax, grand_total, abs_tol=self.TOLERANCE):
                issues.append(
                    ValidationIssue(
                        field="grand_total",
                        status="amount_mismatch",
                        message=f"Subtotal ({subtotal}) + tax ({tax}) does not match grand_total ({grand_total}) within tolerance",
                        confidence=0.45,
                    )
                )

        elif doc_type == DocumentType.bank_statement:
            txs = data.get("transactions") or []
            if len(txs) > 1:
                # Detect flow direction: Ascending (chronological) vs Descending (reverse chronological)
                asc_matches = 0
                desc_matches = 0
                for i in range(1, len(txs)):
                    prev_bal = self._num(txs[i - 1].get("balance"))
                    curr_bal = self._num(txs[i].get("balance"))

                    debit_curr = self._num(txs[i].get("debit"))
                    credit_curr = self._num(txs[i].get("credit"))

                    debit_prev = self._num(txs[i - 1].get("debit"))
                    credit_prev = self._num(txs[i - 1].get("credit"))

                    # If Ascending: curr_bal = prev_bal + credit_curr - debit_curr
                    if math.isclose(curr_bal, prev_bal + credit_curr - debit_curr, abs_tol=self.TOLERANCE):
                        asc_matches += 1
                    # If Descending: curr_bal = prev_bal - credit_prev + debit_prev
                    if math.isclose(curr_bal, prev_bal - credit_prev + debit_prev, abs_tol=self.TOLERANCE):
                        desc_matches += 1

                is_ascending = asc_matches >= desc_matches

                # Validate each step using the detected direction
                for i in range(1, len(txs)):
                    prev_bal = self._num(txs[i - 1].get("balance"))
                    curr_bal = self._num(txs[i].get("balance"))

                    if is_ascending:
                        debit_curr = self._num(txs[i].get("debit"))
                        credit_curr = self._num(txs[i].get("credit"))
                        expected = prev_bal + credit_curr - debit_curr
                    else:
                        debit_prev = self._num(txs[i - 1].get("debit"))
                        credit_prev = self._num(txs[i - 1].get("credit"))
                        expected = prev_bal - credit_prev + debit_prev

                    if not math.isclose(curr_bal, expected, abs_tol=self.TOLERANCE):
                        issues.append(
                            ValidationIssue(
                                field="transactions",
                                status="balance_mismatch",
                                message=f"Transaction balance progression mismatch at index {i+1}: expected {expected:.2f}, got {curr_bal:.2f}",
                                confidence=0.4,
                            )
                        )

    def _detect_duplicates(self, doc_type: DocumentType, data: dict[str, Any], issues: list[ValidationIssue]) -> None:
        if doc_type in (DocumentType.invoice, DocumentType.purchase_order):
            seen_items: set[tuple[Any, ...]] = set()
            for item in data.get("line_items") or []:
                key = (
                    item.get("description"),
                    item.get("quantity"),
                    item.get("unit_price"),
                    item.get("amount"),
                )
                if key in seen_items:
                    issues.append(
                        ValidationIssue(
                            field="line_items",
                            status="duplicate",
                            message=f"Duplicate line item detected: '{item.get('description')}'",
                            confidence=0.5,
                        )
                    )
                seen_items.add(key)

        elif doc_type == DocumentType.bank_statement:
            seen_txs: set[tuple[Any, ...]] = set()
            for tx in data.get("transactions") or []:
                key = (
                    tx.get("date"),
                    tx.get("description"),
                    tx.get("debit"),
                    tx.get("credit"),
                    tx.get("balance"),
                )
                if key in seen_txs:
                    issues.append(
                        ValidationIssue(
                            field="transactions",
                            status="duplicate",
                            message=f"Duplicate transaction detected: '{tx.get('description')}' on {tx.get('date')}",
                            confidence=0.5,
                        )
                    )
                seen_txs.add(key)

    def _is_valid_date(self, val: Any) -> bool:
        if not val or not isinstance(val, str):
            return False
        # Clean and test common date formats
        cleaned = val.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%b %d, %Y", "%d %b %Y"):
            try:
                datetime.strptime(cleaned, fmt)
                return True
            except ValueError:
                continue
        return False

    def _is_valid_float(self, val: Any) -> bool:
        if val is None or val == "":
            return True
        if isinstance(val, (int, float)):
            return True
        try:
            self._num(val)
            return True
        except ValueError:
            return False

    def _is_valid_currency(self, val: Any) -> bool:
        if val is None or val == "":
            return True
        cleaned = str(val).strip()
        known = {"INR", "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "₹", "$", "€", "£", "¥"}
        return cleaned in known or (1 <= len(cleaned) <= 4 and cleaned.isalpha())

    def _num(self, value: Any) -> float:
        if value in (None, ""):
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        cleaned = str(value).replace(",", "").replace("₹", "").replace("$", "").replace("€", "").replace("£", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            raise ValueError(f"Cannot parse float value: {value}")

