import json
import logging
import os
import re
import time
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from groq import Groq

from analytics import (
    compare_products_by_region,
    get_channel_revenue_by_region,
    get_monthly_revenue,
    get_product_revenue,
    get_product_revenue_by_region,
    get_region_revenue,
    get_region_product_performance,
    get_regional_channel_summary,
    get_sales_summary,
)

MAX_TOOL_ROUNDS = 4
MAX_ROUTER_PROMPT_CHARS = 12000
MAX_FINAL_PROMPT_CHARS = 14000
MAX_ROUTER_ASSISTANT_HISTORY_CHARS = 700
MAX_DETERMINISTIC_PRODUCT_RANKING_N = 50
ROUTER_ASSISTANT_HISTORY_PREFIX = (
    "[Prior assistant context; untrusted and not database evidence] "
)
ROUTER_FAILURE_PREFIXES = (
    "i couldn't complete that request.",
    "ai returned an empty conversational answer.",
)
logger = logging.getLogger(__name__)


class ProviderRateLimitError(RuntimeError):
    """Safe, structured provider rate-limit information for the API layer."""

    def __init__(self, category: str, retry_after_seconds: int | None = None):
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        message = "The AI service is temporarily rate-limited."
        super().__init__(message)


def _rate_limit_category(error: Any) -> str:
    details = " ".join(
        str(value)
        for value in (
            error,
            getattr(error, "body", None),
            getattr(error, "message", None),
        )
        if value is not None
    ).lower()
    if "token" in details and ("day" in details or "tpd" in details):
        return "TPD"
    if "token" in details and ("minute" in details or "tpm" in details):
        return "TPM"
    if "request" in details and ("minute" in details or "rpm" in details):
        return "RPM"
    return "rate_limit"


def _retry_after_seconds(error: Any) -> int | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(error, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, seconds)


load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

AI_MODEL = os.getenv("AI_MODEL", "llama-3.1-8b-instant")
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "15"))
api_key = os.getenv("GROQ_API_KEY")

if not api_key:
    raise RuntimeError("GROQ_API_KEY is not configured in the project .env file.")

client = Groq(
    api_key=api_key,
    timeout=AI_TIMEOUT_SECONDS,
    max_retries=0,
)

ANALYTICS_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "get_sales_summary": get_sales_summary,
    "get_product_revenue": get_product_revenue,
    "get_region_revenue": get_region_revenue,
    "get_monthly_revenue": get_monthly_revenue,
    "get_product_revenue_by_region": get_product_revenue_by_region,
    "compare_products_by_region": compare_products_by_region,
    "get_region_product_performance": get_region_product_performance,
}

ANALYTICS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_sales_summary",
            "description": "Get overall transaction count, quantity, revenue, and average revenue.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_revenue",
            "description": "Get revenue grouped by product, sorted highest first.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_region_revenue",
            "description": "Get revenue grouped by region, sorted highest first.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_monthly_revenue",
            "description": "Get revenue grouped by month, sorted chronologically.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_revenue_by_region",
            "description": "Get product revenue, quantity sold, and transaction count for one region.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "The exact region to analyze."},
                },
                "required": ["region"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_products_by_region",
            "description": "Compare selected products, or all products, within one region.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "The exact region to analyze."},
                    "products": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional product names to compare. Omit to compare all products.",
                    },
                },
                "required": ["region"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_region_product_performance",
            "description": "Get a region and its products ordered by revenue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {"type": "string", "description": "The exact region to analyze."},
                },
                "required": ["region"],
                "additionalProperties": False,
            },
        },
    },
]


def _select_planning_tools(
    question: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Select a conservative approved-tool subset for the planning request."""
    raw_question = question
    current_question_signal = re.search(
        r"\b(revenue|sales|product|products|region|regions|monthly|month|"
        r"trend|quantity|units|transactions|performance|compare|comparison|"
        r"ranking|highest|lowest|top|best|second-highest|second highest|"
        r"difference|percentage|percent|share|ratio|proportion)\b",
        raw_question.lower(),
    )
    if not current_question_signal:
        return ANALYTICS_TOOLS
    context_parts = [
        raw_question,
        *[
            message["content"]
            for message in (conversation_history or [])
            if (
                isinstance(message, dict)
                and isinstance(message.get("content"), str)
            )
        ],
    ]
    raw_context = " ".join(context_parts)
    context = raw_context.lower()
    data_signal = re.search(
        r"\b(revenue|sales|product|products|region|regions|monthly|month|"
        r"trend|quantity|units|transactions|performance|compare|comparison|"
        r"ranking|highest|lowest|top|best|second-highest|second highest)\b",
        context,
    )
    if not data_signal:
        return ANALYTICS_TOOLS

    names: set[str] = set()
    has_region = bool(
        re.search(r"\bregion(?:s|al)?\b", context)
        or re.search(
            r"\b(?:in|for|within)\s+[A-Z][A-Za-z0-9-]*\b",
            raw_context,
        )
    )
    has_product = bool(re.search(r"\bproduct(?:s)?\b", context))
    has_monthly = bool(re.search(r"\b(month|monthly|trend|over time)\b", context))
    has_comparison = bool(
        re.search(r"\b(compare|comparison|versus|vs\.?|difference|first two)\b", context)
    )
    has_second_region = bool(
        re.search(r"\bsecond[- ]highest\b", context)
        and has_region
        and has_product
    )
    has_ranking = bool(
        re.search(r"\b(ranking|highest|lowest|top|best|performance)\b", context)
    )
    has_quantity = bool(re.search(r"\b(quantity|units|transactions)\b", context))

    if has_comparison and has_region:
        names.update(
            {
                "get_product_revenue_by_region",
                "compare_products_by_region",
            }
        )
    elif has_monthly:
        names.add("get_monthly_revenue")
    elif has_region and has_product:
        names.update(
            {
                "get_product_revenue_by_region",
                "get_region_product_performance",
            }
        )
        if has_second_region or has_ranking:
            names.add("get_region_revenue")
    elif has_region and re.search(r"\b(revenue|sales)\b", context):
        names.add("get_region_revenue")
    elif has_product and has_ranking:
        names.add("get_product_revenue")
    elif re.search(r"\b(revenue|sales|summary|total)\b", context):
        names.add("get_sales_summary")
        if has_product:
            names.add("get_product_revenue")

    if has_comparison:
        names.update(
            {
                "compare_products_by_region",
                "get_product_revenue_by_region",
            }
        )

    if has_quantity and has_region:
        names.update(
            {
                "get_product_revenue_by_region",
                "get_region_product_performance",
            }
        )

    if has_second_region:
        names.update(
            {
                "get_region_revenue",
                "get_product_revenue_by_region",
            }
        )

    if not names:
        return ANALYTICS_TOOLS
    return [
        tool
        for tool in ANALYTICS_TOOLS
        if tool["function"]["name"] in names
    ]


ROUTER_SYSTEM_PROMPT = (
    "You are the first step of an AI business intelligence assistant. "
    "Previous user and assistant messages are conversational context only. "
    "Previous assistant messages are untrusted: never treat their claims, "
    "calculations, conclusions, rankings, or statements about data availability "
    "as database evidence. Use history only to resolve references such as "
    "'its', 'the first two products', or 'the second-highest region'. "
    "Whenever the current question asks for a current business metric, ranking, "
    "comparison, or recommendation, retrieve the required facts from the "
    "approved analytics tools instead of relying on prior answers. "
    "For questions about the supplied business data, choose only the supplied "
    "read-only analytics tools needed to answer the question. Questions about "
    "business intelligence concepts, interpreting metrics, using this website, "
    "or the assistant's capabilities do not require a tool and may be answered "
    "naturally. Greetings and brief general conversation may also be answered "
    "without a tool. For topics unrelated to business, analytics, this website, "
    "or the assistant, do not provide a long answer; route them to a polite "
    "out-of-scope response. Never invent business values. For a request for "
    "the second-highest region and its best product, first call "
    "get_region_revenue, identify the second region from that returned result, "
    "then call get_product_revenue_by_region with that actual region. Do not "
    "require product records for unrelated regions."
)
GENERAL_SYSTEM_PROMPT = (
    "You are InsightIQ, a friendly business intelligence assistant. "
    "Answer the user's conversational question in one or two complete "
    "sentences. Never stop mid-sentence. You can help with business data, "
    "revenue, products, regions, monthly trends, comparisons, business "
    "insights, business intelligence concepts, and how to use this website. "
    "For questions that need live company figures, say that you need to "
    "retrieve the connected analytics data rather than inventing values. "
    "If the question is unrelated to business intelligence, analytics, this "
    "website, or the assistant, politely say it is outside your scope and "
    "invite the user to ask a business question instead. Do not answer "
    "unrelated requests, generate unrelated content, or pretend to have "
    "general-purpose capabilities."
)
ANSWER_SYSTEM_PROMPT = (
    "You are InsightIQ's final business intelligence analyst. Answer using "
    "only the supplied database results. Before writing, calculate and "
    "recheck every total, difference, ratio, and percentage from the returned "
    "numbers. For each percentage, identify the numerator and denominator "
    "and use the exact formula (numerator / denominator) * 100; do not use "
    "an estimate or a rounded guess. Use the supplied verified calculations "
    "when available; never reuse a percentage from a different comparison. "
    "Only claim that a product has the highest quantity sold when it is first "
    "in the supplied complete quantity ranking. Label it as regional only "
    "when the ranking is for that region, and label it as overall only when "
    "a complete overall product ranking is supplied. If the required scope "
    "or ranking is unavailable, omit the claim. Never infer price elasticity "
    "from quantity and revenue alone. Customer retention, complementary "
    "demand, profitability, and revenue growth are not established by these "
    "metrics; describe them only as hypotheses requiring measurement. "
    "A higher-revenue region does not establish a larger market, higher demand, "
    "better sales coverage, more effective marketing, or better sales "
    "processes. A lower-revenue region does not establish the cause of the "
    "difference or prove that it needs more support. Never state or imply any "
    "of those regional interpretations as facts. "
    "Revenue divided by quantity is only an average revenue-per-unit "
    "calculation. Never call it profit margin, high-margin, low-margin, "
    "premium-priced, or loss-leading without direct cost, margin, and pricing "
    "evidence. Quantity divided by transactions is not evidence about "
    "individual customers or purchasing behavior; when transactions are "
    "available, quantity divided by transactions may be reported only as the "
    "aggregate average units per transaction. Pair-share percentages compare "
    "the revenue contributions of the named products only. They do not show "
    "that customers purchased those products together and must not support a "
    "bundling or cross-selling recommendation without transaction-level basket "
    "data. "
    "For sales-channel comparisons, use the terminology 'revenue difference' "
    "or 'revenue advantage'; never use 'profit margin', 'profitability', or "
    "'channel effectiveness' when only revenue data is available. Never equate "
    "higher revenue with higher profitability or channel effectiveness. When "
    "calculating channel percentage differences or advantages, always use the "
    "comparison channel (the base channel being compared against, such as Online) "
    "as the denominator: percentage difference = ((channel_revenue - comparison_revenue) "
    "/ comparison_revenue) * 100. For instance, South Retail revenue advantage "
    "over Online is 6.58% and North Retail revenue advantage over Online is 56.30%. "
    "Cost of goods sold (COGS) and operational cost data are completely unavailable "
    "in this dataset; explicitly identify cost of goods sold as unavailable whenever "
    "profitability is considered, and do not calculate, infer, or claim profitability "
    "without cost data. Available dataset fields include unit_price, discount_amount, "
    "sales_channel, and region; recommendations may analyze average selling prices "
    "and discounts by channel and region. "
    "Distinguish evidence-backed observations from proposed experiments. "
    "Present promotional, discounting, pricing, or marketing initiatives as "
    "experiments whose outcomes must be measured, never as guaranteed improvements. "
    "Structure every business answer with four sections: 'Observed Facts' "
    "for statements directly returned by the database; 'Calculated Insights' "
    "for transparent mathematical derivations from those results; 'Business "
    "Hypotheses' for possible explanations or actions that are explicitly "
    "uncertain; and 'Recommendations' for practical evidence-supported "
    "actions. Never put a calculation, assumption, forecast, or recommendation "
    "in Observed Facts. State observed database facts first and include exact "
    "revenue values with the ₹ symbol and Indian digit grouping, such as "
    "₹1,89,000 or ₹4,11,200; omit unnecessary decimal places for whole-rupee "
    "values and never use western grouping such as ₹411,200. "
    "For a regional comparison, use the supplied regional_comparison facts "
    "verbatim: do not recalculate or alter total revenue, regional revenue, "
    "revenue differences, regional shares, quantities, or transaction counts. "
    "A product's regional revenue share must use that region's total revenue "
    "as its regional revenue denominator, not overall revenue. Never reverse "
    "a supplied product "
    "quantity comparison; specifically, do not claim South sells more Monitor "
    "units when the supplied rows show otherwise. Never invent regional or "
    "product-level transaction counts. Do not claim customer behavior from "
    "regional revenue, quantity, or transaction rows. "
    "Compare products using returned values. Keep overall and regional "
    "metrics separate: get_product_revenue and get_sales_summary are overall "
    "dataset metrics, while get_product_revenue_by_region, "
    "compare_products_by_region, and get_region_product_performance are "
    "region-specific. When the user asks for products in a region, label "
    "the ranking with that region by name. Never use an overall quantity, "
    "revenue, or ranking as evidence for a regional claim or recommendation. "
    "Overall monthly revenue is not region-specific and must never be used "
    "to claim that one region has a seasonal peak, similar regional peaks, "
    "regional seasonality, or a regional cause. "
    "A recommendation about a region must use that region's complete "
    "regional product rows; if those rows are unavailable, label the "
    "recommendation as overall or omit the regional claim. "
    "Every recommendation must be a practical action supported by a specific "
    "observed product, region, or metric returned by an approved tool. Explain "
    "the evidence behind each action, label expected benefits as hypotheses "
    "unless directly measured, and mention additional cost, margin, basket, "
    "customer, or marketing data needed to validate an unmeasured hypothesis. "
    "Do not recommend copying another region's strategy, increasing regional "
    "sales support, reallocating resources, changing marketing, or changing "
    "coverage based only on a revenue ranking or a lower regional revenue "
    "value. Such a result supports, at most, an investigation or data "
    "collection step. If the evidence does not identify an actionable cause, "
    "recommend measuring the relevant cause before taking business action. "
    "Never present an assumption as a fact. Higher sales quantity does not "
    "automatically mean higher profitability; higher revenue does not mean "
    "higher profit; co-occurring product rows do not prove frequent joint "
    "purchases; a pricing change cannot be claimed to improve profitability "
    "without cost and margin data; and marketing investment cannot be claimed "
    "to increase revenue without supporting evidence. Do not claim customer "
    "purchasing patterns, product complementarity, profitability, ROI, profit, "
    "marketing effectiveness, growth, causality, or targets unless the "
    "supplied data directly measures it. Revenue, quantity, and transaction "
    "counts do not establish profit, customer retention, marketing ROI, or "
    "growth. If available data is insufficient to support a recommendation, "
    "state the limitation explicitly and never invent missing metrics. Avoid "
    "generic or repetitive actions; make each recommendation distinct and "
    "tied to returned evidence. Do not recommend price increases, discounts, "
    "bundles, cross-selling, or marketing investment unless the supplied data "
    "directly supports that action; otherwise recommend an investigation or "
    "data-collection step. If the "
    "user requests exactly three recommendations, provide exactly three "
    "numbered, complete recommendations—no fewer and no extras—and include "
    "Action, Supporting evidence, and Expected business benefit (hypothesis "
    "if not measured) in each one. Each recommendation must cite a distinct "
    "observed metric or explicitly state why an investigation is needed; do "
    "not fill missing evidence with a generic action. Finish every sentence "
    "and avoid repetition. "
    "Use concise Markdown when useful."
)


def _verified_derived_metrics(business_data: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic comparisons from product rows returned by analytics."""
    product_rows: dict[str, dict[str, float]] = {}
    complete_region_product_data = False
    comparison_products: list[str] = []
    overall_rows: list[dict[str, float]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("products"), list):
                collect(value["products"])
            elif isinstance(value.get("product"), str):
                try:
                    product_rows[value["product"]] = {
                        "revenue": float(value["total_revenue"]),
                        "quantity": float(value["total_quantity"]),
                        "transactions": float(value["total_transactions"]),
                    }
                except (KeyError, TypeError, ValueError):
                    return
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for function_name in ("get_product_revenue_by_region", "get_region_product_performance"):
        if business_data.get(function_name):
            complete_region_product_data = True
        collect(business_data.get(function_name))
    overall_result = business_data.get("get_product_revenue")
    if isinstance(overall_result, list):
        for row in overall_result:
            if not isinstance(row, dict) or not isinstance(row.get("product"), str):
                continue
            try:
                overall_rows.append(
                    {
                        "product": row["product"],
                        "revenue": float(row["total_revenue"]),
                        "quantity": float(row["total_quantity"]),
                        "transactions": float(row["total_transactions"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                overall_rows = []
                break
    comparison_results = business_data.get("compare_products_by_region")
    collect(comparison_results)
    if isinstance(comparison_results, list):
        comparison_products = [
            row["product"]
            for row in comparison_results
            if isinstance(row, dict) and isinstance(row.get("product"), str)
        ]

    rows = [
        {"product": product, **metrics}
        for product, metrics in product_rows.items()
    ]
    rows_by_revenue = sorted(rows, key=lambda row: row["revenue"], reverse=True)
    rows_by_quantity = (
        sorted(rows, key=lambda row: row["quantity"], reverse=True)
        if complete_region_product_data
        else []
    )
    overall_quantity_ranking = (
        sorted(overall_rows, key=lambda row: row["quantity"], reverse=True)
        if overall_rows
        else []
    )

    pair_comparisons = []
    for first, second in combinations(rows, 2):
        combined = first["revenue"] + second["revenue"]
        if combined == 0:
            continue
        pair_comparisons.append(
            {
                "products": [first["product"], second["product"]],
                "combined_revenue": round(combined, 2),
                "shares": [
                    {
                        "product": first["product"],
                        "numerator": round(first["revenue"], 2),
                        "denominator": round(combined, 2),
                        "percentage": round(first["revenue"] / combined * 100, 2),
                    },
                    {
                        "product": second["product"],
                        "numerator": round(second["revenue"], 2),
                        "denominator": round(combined, 2),
                        "percentage": round(second["revenue"] / combined * 100, 2),
                    },
                ],
            }
        )

    if not comparison_products:
        comparison_products = [
            row["product"] for row in rows_by_revenue[:3]
        ]
    comparison_rows = [
        row for row in rows if row["product"] in comparison_products
    ]
    combined_pair_comparisons = []
    for product in rows:
        other_products = [
            row
            for row in comparison_rows
            if row["product"] != product["product"]
        ]
        for first, second in combinations(other_products, 2):
            combined = first["revenue"] + second["revenue"]
            if combined == 0:
                continue
            combined_pair_comparisons.append(
                {
                    "product": product["product"],
                    "numerator": round(product["revenue"], 2),
                    "compared_with": [first["product"], second["product"]],
                    "denominator": round(combined, 2),
                    "percentage": round(product["revenue"] / combined * 100, 2),
                }
            )

    return {
        "revenue_ranking": rows_by_revenue,
        "quantity_ranking": rows_by_quantity,
        "quantity_ranking_complete": complete_region_product_data,
        "overall_quantity_ranking": overall_quantity_ranking,
        "overall_quantity_ranking_complete": bool(overall_rows),
        "comparison_products": comparison_products,
        "pair_revenue_shares": pair_comparisons,
        "product_vs_combined_pair_percentages": combined_pair_comparisons,
    }


def _select_relevant_verified_calculations(
    question: str,
    calculations: dict[str, Any],
) -> dict[str, Any]:
    """Select only calculations relevant to the current answer question."""
    normalized = question.lower()
    has_revenue = bool(re.search(r"\b(revenue|sales|turnover)\b", normalized))
    has_quantity = bool(
        re.search(r"\b(quantity|quantities|units|sold|volume)\b", normalized)
    )
    has_comparison = bool(
        re.search(r"\b(compare|comparison|versus|vs\.?|difference|first two)\b", normalized)
    )
    has_percentage = bool(
        re.search(r"\b(percentage|percent|share|ratio|proportion)\b", normalized)
    )
    has_recommendation = bool(
        re.search(
            r"\b(recommend|recommendation|recommendations|strategy|strategies|"
            r"action|actions|suggest|suggestions)\b",
            normalized,
        )
    )
    has_ranking = bool(
        re.search(
            r"\b(rank|ranking|highest|lowest|top|best|worst|leading|"
            r"second-highest|second highest)\b",
            normalized,
        )
    )
    has_analytics_signal = bool(
        re.search(
            r"\b(revenue|sales|turnover|quantity|quantities|units|sold|"
            r"volume|product|products|region|regions|transaction|"
            r"transactions|monthly|month|performance|business data|"
            r"analytics|compare|comparison|versus|difference|percentage|"
            r"percent|share|ratio|proportion|first two)\b",
            normalized,
        )
    )
    if not any(
        (has_revenue, has_quantity, has_comparison, has_percentage,
         has_recommendation, has_ranking)
    ) or not has_analytics_signal:
        return calculations

    selected: dict[str, Any] = {}
    if has_quantity:
        selected["quantity_ranking"] = calculations.get("quantity_ranking", [])
        selected["quantity_ranking_complete"] = calculations.get(
            "quantity_ranking_complete",
            False,
        )
        selected["overall_quantity_ranking"] = calculations.get(
            "overall_quantity_ranking",
            [],
        )
        selected["overall_quantity_ranking_complete"] = calculations.get(
            "overall_quantity_ranking_complete",
            False,
        )
    if has_revenue or has_ranking or has_recommendation:
        selected["revenue_ranking"] = calculations.get("revenue_ranking", [])
    if has_comparison or has_percentage:
        comparison_products = calculations.get(
            "comparison_products",
            [],
        )
        selected["comparison_products"] = comparison_products
        relevant_products = {
            product
            for product in comparison_products
            if isinstance(product, str)
        }
        pair_shares = calculations.get("pair_revenue_shares", [])
        selected["pair_revenue_shares"] = [
            item
            for item in pair_shares
            if isinstance(item, dict)
            and set(item.get("products", [])) <= relevant_products
        ]
        combined_pairs = calculations.get(
            "product_vs_combined_pair_percentages",
            [],
        )
        if has_percentage:
            selected["product_vs_combined_pair_percentages"] = [
                item
                for item in combined_pairs
                if isinstance(item, dict)
                and (
                    item.get("product") in relevant_products
                    or bool(
                        set(item.get("compared_with", []))
                        & relevant_products
                    )
                )
            ]
    if has_recommendation:
        selected.setdefault(
            "quantity_ranking",
            calculations.get("quantity_ranking", []),
        )
        selected.setdefault(
            "quantity_ranking_complete",
            calculations.get("quantity_ranking_complete", False),
        )
        selected.setdefault(
            "overall_quantity_ranking_complete",
            calculations.get("overall_quantity_ranking_complete", False),
        )
    for key in calculations:
        if key.endswith("_complete") or "tie" in key.lower():
            selected[key] = calculations[key]
    return selected


def _log_response(response: Any, stage: str, round_number: int | None) -> None:
    try:
        choice = response.choices[0]
        message = choice.message
        tool_names = [
            call.function.name
            for call in (message.tool_calls or [])
            if getattr(call, "function", None) is not None
        ]
        logger.info(
            "Groq response stage=%s round=%s finish_reason=%s content_present=%s "
            "content_length=%s tool_names=%s",
            stage,
            round_number if round_number is not None else "-",
            getattr(choice, "finish_reason", None),
            bool((message.content or "").strip()),
            len(message.content or ""),
            tool_names,
        )
    except (AttributeError, IndexError, TypeError):
        logger.warning(
            "Groq response stage=%s round=%s had an unexpected response shape",
            stage,
            round_number if round_number is not None else "-",
        )


def _chat_completion(
    messages: list[dict[str, Any]],
    *,
    stage: str,
    round_number: int | None = None,
    **kwargs: Any,
) -> Any:
    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=messages,
            **kwargs,
        )
        _log_response(response, stage, round_number)
        return response
    except Exception as error:
        response = getattr(error, "response", None)
        status_code = getattr(error, "status_code", None)
        if status_code is None:
            status_code = getattr(response, "status_code", None)
        logger.error(
            "Groq provider error stage=%s round=%s error_type=%s status=%s",
            stage,
            round_number if round_number is not None else "-",
            type(error).__name__,
            status_code,
        )
        if status_code == 429:
            raise ProviderRateLimitError(
                _rate_limit_category(error),
                _retry_after_seconds(error),
            ) from error
        raise RuntimeError("AI provider request failed.") from error


def _parse_tool_arguments(tool_name: str, raw_arguments: str) -> dict[str, Any]:
    try:
        arguments = json.loads(raw_arguments or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Invalid arguments for approved analytics function: {tool_name}"
        ) from error

    if not isinstance(arguments, dict):
        raise RuntimeError(f"Arguments for {tool_name} must be a JSON object.")

    required_arguments = {
        "get_product_revenue_by_region": {"region"},
        "compare_products_by_region": {"region"},
        "get_region_product_performance": {"region"},
    }.get(tool_name, set())
    allowed_arguments = {
        "get_sales_summary": set(),
        "get_product_revenue": set(),
        "get_region_revenue": set(),
        "get_monthly_revenue": set(),
        "get_product_revenue_by_region": {"region"},
        "compare_products_by_region": {"region", "products"},
        "get_region_product_performance": {"region"},
    }.get(tool_name)
    if allowed_arguments is None:
        raise RuntimeError(
            f"AI requested an unapproved analytics function: {tool_name}"
        )

    unknown_arguments = set(arguments) - allowed_arguments
    missing_arguments = required_arguments - set(arguments)
    if unknown_arguments:
        raise RuntimeError(
            f"Unexpected arguments for {tool_name}: {', '.join(sorted(unknown_arguments))}"
        )
    if missing_arguments:
        raise RuntimeError(
            f"Missing arguments for {tool_name}: {', '.join(sorted(missing_arguments))}"
        )
    if "region" in arguments and (
        not isinstance(arguments["region"], str) or not arguments["region"].strip()
    ):
        raise RuntimeError(f"Argument 'region' for {tool_name} must be a non-empty string.")
    if "products" in arguments and arguments["products"] is not None:
        if (
            not isinstance(arguments["products"], list)
            or any(not isinstance(product, str) for product in arguments["products"])
        ):
            raise RuntimeError(
                f"Argument 'products' for {tool_name} must be an array of strings."
            )

    return arguments


def _final_answer_prompt(question: str) -> str:
    """Add an explicit count constraint when the user requests three actions."""
    normalized_question = question.lower()
    asks_for_three = bool(
        re.search(
            r"\b(?:three|3)\s+(?:business\s+)?(?:recommendations?|strategies?)\b"
            r"|\brecommend\s+(?:exactly\s+)?(?:three|3)\b",
            normalized_question,
        )
    )
    if asks_for_three:
        return (
            f"{ANSWER_SYSTEM_PROMPT} The user's request contains a three-"
            "recommendation requirement. Enforce exactly three numbered items "
            "in the final answer."
        )
    return ANSWER_SYSTEM_PROMPT


def _compact_final_answer_context(
    question: str,
    business_data: dict[str, Any],
    regional_context: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """Keep each returned database row set once in the final prompt."""
    regional_tools = {
        "get_product_revenue_by_region",
        "compare_products_by_region",
        "get_region_product_performance",
    }
    compact_context = {
        name: value
        for name, value in business_data.items()
        if name not in regional_tools
    }
    regional_results = dict(regional_context or {})
    if not regional_results:
        for tool_name in regional_tools:
            result = business_data.get(tool_name)
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, list):
                        for row in item:
                            if isinstance(row, dict) and isinstance(
                                row.get("region"), str
                            ):
                                regional_results.setdefault(row["region"], item)
                    elif isinstance(item, dict) and isinstance(
                        item.get("region"), str
                    ):
                        regional_results.setdefault(item["region"], result)
            elif isinstance(result, dict):
                region = result.get("region")
                rows = result.get("products")
                if isinstance(region, str) and isinstance(rows, list):
                    regional_results.setdefault(region, rows)
    if regional_results:
        compact_context["regional_results"] = regional_results
    verified_calculations = _verified_derived_metrics(business_data)
    compact_context["verified_calculations"] = _select_relevant_verified_calculations(
        question,
        verified_calculations,
    )
    compact_context["scope_rules"] = {
        "overall_tools": [
            "get_sales_summary",
            "get_product_revenue",
            "get_region_revenue",
            "get_monthly_revenue",
        ],
        "regional_results_are_scoped_by_region": True,
        "regional_recommendations_require_matching_complete_rows": True,
        "overall_data_must_not_support_regional_claims": True,
        "quantity_rankings_require_complete_source_data": True,
        "answer_sections": [
            "Observed Facts",
            "Calculated Insights",
            "Business Hypotheses",
            "Recommendations",
        ],
    }
    return compact_context


def _final_answer_messages(
    question: str,
    business_data: dict[str, Any],
    regional_context: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, str]]:
    grounded_context = _compact_final_answer_context(
        question,
        business_data,
        regional_context,
    )
    result_message = (
        "Validated database results from the approved read-only "
        "analytics tools, followed by deterministic calculations "
        "from those results. Use only matching calculations:\n"
        f"{json.dumps(grounded_context, default=str)}"
    )
    if len(result_message) > MAX_FINAL_PROMPT_CHARS:
        raise RuntimeError(
            "Validated analytics context exceeds the AI prompt-size limit."
        )
    return [
        {"role": "system", "content": _final_answer_prompt(question)},
        {"role": "user", "content": question},
        {
            "role": "user",
            "content": result_message,
        },
    ]


def _is_second_region_best_product_intent(question: str) -> bool:
    normalized = re.sub(r"[-\u2011]", " ", question.lower())
    return bool(
        re.search(r"\bsecond\s+highest\b", normalized)
        and re.search(r"\bregion\b", normalized)
        and re.search(r"\b(product|performs?|best|top)\b", normalized)
    )


def _is_overall_summary_intent(question: str) -> bool:
    """Recognize only explicit, unscoped overall summary questions."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or re.search(
        r"\b(region|regional|product|monthly|month|north|south|east|west)\b",
        normalized,
    ):
        return False
    return bool(
        re.fullmatch(
            r"(?:please\s+)?(?:what\s+is\s+the\s+)?total\s+revenue",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?(?:what\s+is\s+the\s+)?total\s+sales",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?how\s+many\s+transactions\s+are\s+there",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?(?:what\s+is\s+the\s+)?total\s+quantity\s+sold",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?(?:what\s+is\s+the\s+)?average\s+revenue\s+per\s+transaction",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?(?:give\s+me|show)\s+(?:an?|the)\s+overall\s+business\s+summary",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?(?:give\s+me|show)\s+overall\s+business\s+summary",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?summarize\s+(?:the\s+)?business(?:\s+performance|\s+summary)?",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?what\s+is\s+(?:the\s+)?overall\s+business\s+performance",
            normalized,
        )
        or re.fullmatch(
            r"(?:please\s+)?overall\s+business\s+summary",
            normalized,
        )
    )


def _format_inr_currency(amount: float | int) -> str:
    """Format a monetary amount in Indian Rupees (₹) with Indian digit grouping."""
    rounded = int(round(float(amount)))
    s = str(rounded)
    if len(s) <= 3:
        return f"₹{s}"
    last_three = s[-3:]
    other_digits = s[:-3]
    res = ""
    while len(other_digits) > 2:
        res = "," + other_digits[-2:] + res
        other_digits = other_digits[:-2]
    res = other_digits + res + "," + last_three
    return f"₹{res}"


def _format_offline_overall_summary(summary_data: dict[str, Any]) -> str:
    """Generate a clean factual response for overall business summary without calling Groq."""
    tx = summary_data.get("total_transactions", 0)
    qty = summary_data.get("total_quantity", 0)
    rev = summary_data.get("total_revenue", 0.0)
    avg_rev = summary_data.get("average_revenue", 0.0)
    formatted_rev = _format_inr_currency(rev)
    formatted_avg = _format_inr_currency(avg_rev)
    formatted_qty = f"{int(round(qty)):,}" if isinstance(qty, (int, float)) else str(qty)

    return (
        f"### Observed Facts\n"
        f"- **Total Revenue**: {formatted_rev}\n"
        f"- **Completed Transactions**: {tx}\n"
        f"- **Total Quantity Sold**: {formatted_qty} units\n"
        f"- **Average Revenue per Transaction**: {formatted_avg}\n\n"
        f"### Calculated Insights\n"
        f"- The business has recorded {tx} completed sales transactions totaling {formatted_rev} in revenue.\n"
        f"- Average order value stands at {formatted_avg} per transaction across {formatted_qty} total units sold."
    )


def _format_offline_revenue_extreme(
    dataset: str,
    direction: str,
    data: dict[str, Any],
) -> str:
    """Generate a clean factual response for revenue extreme questions without calling Groq."""
    analysis = data.get("deterministic_analysis", {})
    winners = analysis.get("winners", [])
    value = analysis.get("value", 0.0)
    winner_str = ", ".join(winners) if winners else "None"
    formatted_val = _format_inr_currency(value)
    label = "Product" if dataset == "product" else ("Region" if dataset == "region" else "Month")
    dir_label = "Highest" if direction == "highest" else "Lowest"

    return (
        f"### Observed Facts\n"
        f"- **{dir_label}-Revenue {label}**: {winner_str}\n"
        f"- **Total Revenue Generated**: {formatted_val}\n\n"
        f"### Calculated Insights\n"
        f"- {winner_str} generates the {direction} revenue among all recorded {dataset}s with a total of {formatted_val}."
    )


def _format_offline_regional_comparison(
    question: str,
    evidence: dict[str, Any],
) -> str:
    """Generate a clean factual response for regional comparisons and sales-channel breakdowns without calling Groq."""
    rc = evidence.get("regional_comparison", {})
    higher_region = rc.get("higher_revenue_region", "")
    comp_region = rc.get("comparison_region", "")
    higher_rev = rc.get("higher_region_revenue", 0.0)
    comp_rev = rc.get("comparison_region_revenue", 0.0)
    rev_diff = rc.get("revenue_difference", 0.0)
    cb = rc.get("channel_breakdown", {})
    advantages = rc.get("channel_revenue_advantages", {})

    is_channel_focused = bool(
        re.search(r"\b(?:sales\s+)?channel\b", question, flags=re.IGNORECASE)
    )

    if is_channel_focused and cb:
        lines = ["### Observed Facts"]
        for reg in (higher_region, comp_region):
            if reg in cb:
                channel_rows = cb[reg]
                row_strs = [
                    f"{r['sales_channel']}: {_format_inr_currency(r['total_revenue'])} ({r['total_quantity']} units, {r['total_transactions']} tx)"
                    for r in channel_rows
                ]
                lines.append(f"- **{reg} Channels**: " + "; ".join(row_strs))

        lines.append("\n### Calculated Insights")
        for reg in (higher_region, comp_region):
            if reg in advantages:
                adv = advantages[reg]
                ret_rev = _format_inr_currency(adv['retail_revenue'])
                onl_rev = _format_inr_currency(adv['online_revenue'])
                diff_str = _format_inr_currency(adv['revenue_difference'])
                pct = adv['retail_revenue_advantage_percentage']
                lines.append(
                    f"- In **{reg}**, Retail generated {ret_rev} compared to Online's {onl_rev}, "
                    f"yielding a Retail revenue advantage of {diff_str} (+{pct:.2f}% over Online)."
                )

        lines.append(
            "\n*(Note: Cost of goods sold is unavailable; revenue advantage reflects sales performance and does not measure profitability or profit margins.)*"
        )
        return "\n".join(lines)

    # Standard regional comparison (revenue comparison between two regions)
    lines = [
        "### Observed Facts",
        f"- **{higher_region} Total Revenue**: {_format_inr_currency(higher_rev)}",
        f"- **{comp_region} Total Revenue**: {_format_inr_currency(comp_rev)}",
        f"- **Revenue Difference**: {_format_inr_currency(rev_diff)}",
    ]

    totals = rc.get("regional_totals", {})
    if higher_region in totals and comp_region in totals:
        h_tot = totals[higher_region]
        c_tot = totals[comp_region]
        lines.append(
            f"- **Volume**: {higher_region} sold {h_tot.get('total_quantity', 0)} units across {h_tot.get('total_transactions', 0)} transactions; "
            f"{comp_region} sold {c_tot.get('total_quantity', 0)} units across {c_tot.get('total_transactions', 0)} transactions."
        )

    lines.append("\n### Calculated Insights")
    shares = rc.get("revenue_shares", {})
    if higher_region in shares and comp_region in shares:
        h_share = shares[higher_region]
        c_share = shares[comp_region]
        lines.append(
            f"- {higher_region} contributes {h_share:.2f}% of overall revenue, whereas {comp_region} contributes {c_share:.2f}%."
        )
    lines.append(
        f"- {higher_region} leads {comp_region} by a revenue difference of {_format_inr_currency(rev_diff)}."
    )

    if cb:
        lines.append("- **Channel Breakdown**:")
        for reg in (higher_region, comp_region):
            if reg in cb:
                ch_details = [
                    f"{c['sales_channel']} ({_format_inr_currency(c['total_revenue'])})"
                    for c in cb[reg]
                ]
                lines.append(f"  - **{reg}**: " + ", ".join(ch_details))

    return "\n".join(lines)


def _deterministic_overall_summary(
    execution_cache: dict[str, Any],
) -> dict[str, Any]:
    """Fetch the explicit overall summary exactly once for this request."""
    cache_key = json.dumps(
        {"function": "get_sales_summary", "arguments": {}},
        sort_keys=True,
    )
    if cache_key not in execution_cache:
        execution_cache[cache_key] = ANALYTICS_FUNCTIONS["get_sales_summary"]()
    summary = execution_cache[cache_key]
    if not isinstance(summary, dict):
        raise RuntimeError("Overall sales summary returned an invalid result.")
    return {"get_sales_summary": summary}


def _revenue_extreme_intent(question: str) -> tuple[str, str] | None:
    """Return (dataset, direction) only for explicit overall revenue extremes."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or re.search(
        r"\b(regional|north|south|east|west|monthly)\b",
        normalized,
    ):
        return None

    direction = "highest" if re.search(r"\b(highest|most)\b", normalized) else None
    if direction is None and re.search(r"\b(lowest|least)\b", normalized):
        direction = "lowest"
    if direction is None or not re.search(r"\brevenue\b", normalized):
        return None

    if re.fullmatch(
        r"(?:which\s+)?(?:product|products)\s+"
        r"(?:generates?|has|with)\s+(?:the\s+)?"
        r"(?:highest|lowest|most|least)\s+revenue",
        normalized,
    ) or re.fullmatch(
        r"(?:what\s+is\s+the\s+)?(?:the\s+)?(?:highest|lowest)\s+revenue\s+"
        r"(?:product|products)",
        normalized,
    ):
        return "product", direction
    if re.fullmatch(
        r"(?:which\s+)?region\s+(?:generates?|has|with)\s+"
        r"(?:the\s+)?(?:highest|lowest|most|least)\s+revenue",
        normalized,
    ) or re.fullmatch(
        r"(?:what\s+is\s+the\s+)?(?:the\s+)?(?:highest|lowest)\s+revenue\s+region",
        normalized,
    ):
        return "region", direction
    if re.fullmatch(
        r"(?:which\s+)?month\s+(?:had|has|with)\s+"
        r"(?:the\s+)?(?:highest|lowest|most|least)\s+revenue",
        normalized,
    ) or re.fullmatch(
        r"(?:what\s+is\s+the\s+)?(?:the\s+)?(?:highest|lowest)\s+revenue\s+month",
        normalized,
    ):
        return "month", direction
    return None


def _deterministic_revenue_extreme(
    dataset: str,
    direction: str,
    execution_cache: dict[str, Any],
) -> dict[str, Any]:
    """Fetch one revenue dataset and retain every tied extreme."""
    function_names = {
        "product": "get_product_revenue",
        "region": "get_region_revenue",
        "month": "get_monthly_revenue",
    }
    function_name = function_names[dataset]
    cache_key = json.dumps(
        {"function": function_name, "arguments": {}},
        sort_keys=True,
    )
    if cache_key not in execution_cache:
        execution_cache[cache_key] = ANALYTICS_FUNCTIONS[function_name]()
    rows = execution_cache[cache_key]
    if not isinstance(rows, list):
        raise RuntimeError(f"{function_name} returned an invalid result.")

    label_key = {"product": "product", "region": "region", "month": "order_month"}[
        dataset
    ]
    valid_rows = [
        row
        for row in rows
        if (
            isinstance(row, dict)
            and isinstance(row.get(label_key), str)
            and isinstance(row.get("total_revenue"), (int, float))
        )
    ]
    if not valid_rows:
        raise RuntimeError(f"No revenue data is available for {dataset}.")

    extreme_value = (
        max(float(row["total_revenue"]) for row in valid_rows)
        if direction == "highest"
        else min(float(row["total_revenue"]) for row in valid_rows)
    )
    winners = [
        row[label_key]
        for row in valid_rows
        if float(row["total_revenue"]) == extreme_value
    ]
    analysis = {
        "intent": f"{direction}_revenue_{dataset}",
        "metric": "total_revenue",
        "direction": direction,
        "value": extreme_value,
        "winners": winners,
        "tie": len(winners) > 1,
    }
    return {
        function_name: rows,
        "deterministic_analysis": analysis,
    }


def _product_ranking_intent(
    question: str,
) -> tuple[str, int | None, bool] | None:
    """Return (direction, N, comparison) for explicit overall rankings."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or re.search(
        r"\b(regional|region|north|south|east|west|monthly|month|quantity|"
        r"profit|profitability|customer|customers)\b",
        normalized,
    ):
        return None

    top_match = re.fullmatch(
        r"(?:show|display|list)\s+the\s+top\s+(\d+)\s+products?\s+by\s+revenue",
        normalized,
    )
    bottom_match = re.fullmatch(
        r"(?:show|display|list)\s+the\s+bottom\s+(\d+)\s+products?\s+by\s+revenue",
        normalized,
    )
    if top_match or bottom_match:
        count = int((top_match or bottom_match).group(1))
        if count < 1 or count > MAX_DETERMINISTIC_PRODUCT_RANKING_N:
            raise RuntimeError(
                "The requested product ranking size must be between 1 and "
                f"{MAX_DETERMINISTIC_PRODUCT_RANKING_N}."
            )
        return ("top" if top_match else "bottom", count, False)

    if re.fullmatch(
        r"(?:rank|show|display|list)\s+all\s+products?\s+by\s+revenue",
        normalized,
    ) or re.fullmatch(r"show\s+the\s+product\s+revenue\s+ranking", normalized):
        return "all", None, False

    if re.fullmatch(
        r"compare\s+the\s+revenue\s+of\s+the\s+top\s+two\s+products",
        normalized,
    ):
        return "top", 2, True
    return None


def _deterministic_product_ranking(
    direction: str,
    requested_count: int | None,
    comparison: bool,
    execution_cache: dict[str, Any],
) -> dict[str, Any]:
    """Fetch the overall product rows once and calculate a tie-safe ranking."""
    function_name = "get_product_revenue"
    cache_key = json.dumps(
        {"function": function_name, "arguments": {}},
        sort_keys=True,
    )
    if cache_key not in execution_cache:
        execution_cache[cache_key] = ANALYTICS_FUNCTIONS[function_name]()
    rows = execution_cache[cache_key]
    if not isinstance(rows, list):
        raise RuntimeError("get_product_revenue returned an invalid result.")
    valid_rows = [
        row
        for row in rows
        if (
            isinstance(row, dict)
            and isinstance(row.get("product"), str)
            and isinstance(row.get("total_revenue"), (int, float))
        )
    ]
    if not valid_rows:
        raise RuntimeError("No overall product revenue data is available.")

    ranked_rows = sorted(
        valid_rows,
        key=lambda row: float(row["total_revenue"]),
        reverse=True,
    )
    if direction == "all":
        selected_rows = ranked_rows
        boundary_value = None
        boundary_tie = False
    else:
        ordered_rows = (
            ranked_rows
            if direction == "top"
            else sorted(
                valid_rows,
                key=lambda row: float(row["total_revenue"]),
            )
        )
        boundary_value = float(ordered_rows[requested_count - 1]["total_revenue"])
        selected_rows = [
            row
            for row in ordered_rows
            if (
                float(row["total_revenue"]) >= boundary_value
                if direction == "top"
                else float(row["total_revenue"]) <= boundary_value
            )
        ]
        boundary_tie = sum(
            float(row["total_revenue"]) == boundary_value
            for row in ordered_rows
        ) > 1

    analysis: dict[str, Any] = {
        "intent": (
            "compare_top_two_product_revenue"
            if comparison
            else "overall_product_revenue_ranking"
        ),
        "scope": "overall",
        "metric": "total_revenue",
        "direction": direction,
        "requested_count": requested_count,
        "returned_count": len(selected_rows),
        "boundary_value": boundary_value,
        "boundary_tie": boundary_tie,
        "products": [
            {
                "product": row["product"],
                "total_revenue": float(row["total_revenue"]),
            }
            for row in selected_rows
        ],
    }
    if comparison:
        if len(ranked_rows) < 2:
            raise RuntimeError(
                "At least two products are required for a revenue comparison."
            )
        first, second = ranked_rows[:2]
        analysis["comparison"] = {
            "products": [first["product"], second["product"]],
            "revenue_difference": float(first["total_revenue"])
            - float(second["total_revenue"]),
        }
    return {
        function_name: rows,
        "deterministic_analysis": analysis,
    }


def _regional_product_ranking_intent(
    question: str,
) -> tuple[str, str, int | None] | None:
    """Return (region, direction, N) for explicit regional revenue rankings."""
    preserved = re.sub(r"[^a-zA-Z0-9\s]", " ", question)
    preserved = re.sub(r"\s+", " ", preserved).strip()
    normalized = re.sub(r"[^a-z0-9\s]", " ", question.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    def match_pattern(pattern: str) -> re.Match[str] | None:
        return re.fullmatch(pattern, preserved, flags=re.IGNORECASE)

    patterns = (
        (
            r"(?:show|display|list)\s+the\s+top\s+(\d+)\s+products?\s+"
            r"in\s+(.+?)\s+by\s+revenue",
            "top",
        ),
        (
            r"(?:show|display|list)\s+the\s+bottom\s+(\d+)\s+products?\s+"
            r"in\s+(.+?)\s+by\s+revenue",
            "bottom",
        ),
    )
    for pattern, direction in patterns:
        match = match_pattern(pattern)
        if match:
            count = int(match.group(1))
            if count < 1 or count > MAX_DETERMINISTIC_PRODUCT_RANKING_N:
                raise RuntimeError(
                    "The requested product ranking size must be between 1 and "
                    f"{MAX_DETERMINISTIC_PRODUCT_RANKING_N}."
                )
            return match.group(2).strip(), direction, count

    match = match_pattern(
        r"which\s+product\s+generates\s+the\s+highest\s+revenue\s+in\s+(.+)",
    )
    if match:
        return match.group(1).strip(), "top", 1
    match = match_pattern(
        r"show\s+the\s+lowest[-\s]revenue\s+product\s+in\s+(.+)",
    )
    if match:
        return match.group(1).strip(), "bottom", 1
    match = match_pattern(
        r"rank\s+products?\s+in\s+(.+?)\s+by\s+revenue",
    )
    if match:
        return match.group(1).strip(), "all", None
    match = match_pattern(
        r"(?:show|display|list)\s+all\s+products?\s+in\s+(.+?)\s+by\s+revenue",
    )
    if match:
        return match.group(1).strip(), "all", None
    return None


def _deterministic_regional_product_ranking(
    region: str,
    direction: str,
    requested_count: int | None,
    execution_cache: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Fetch one validated region and calculate its revenue ranking."""
    arguments = _parse_tool_arguments(
        "get_product_revenue_by_region",
        json.dumps({"region": region}),
    )
    function_name = "get_product_revenue_by_region"
    cache_key = json.dumps(
        {"function": function_name, "arguments": arguments},
        sort_keys=True,
    )
    if cache_key not in execution_cache:
        execution_cache[cache_key] = ANALYTICS_FUNCTIONS[function_name](**arguments)
    rows = execution_cache[cache_key]
    if not isinstance(rows, list):
        raise RuntimeError(
            "get_product_revenue_by_region returned an invalid result."
        )
    valid_rows = [
        row
        for row in rows
        if (
            isinstance(row, dict)
            and isinstance(row.get("product"), str)
            and isinstance(row.get("total_revenue"), (int, float))
        )
    ]
    if not valid_rows:
        raise RuntimeError(
            f"No product revenue data is available for {region}."
        )

    ranked_rows = sorted(
        valid_rows,
        key=lambda row: float(row["total_revenue"]),
        reverse=True,
    )
    boundary_value: float | None = None
    boundary_tie = False
    if direction == "all":
        selected_rows = ranked_rows
    else:
        ordered_rows = (
            ranked_rows
            if direction == "top"
            else sorted(
                valid_rows,
                key=lambda row: float(row["total_revenue"]),
            )
        )
        boundary_value = float(ordered_rows[requested_count - 1]["total_revenue"])
        selected_rows = [
            row
            for row in ordered_rows
            if (
                float(row["total_revenue"]) >= boundary_value
                if direction == "top"
                else float(row["total_revenue"]) <= boundary_value
            )
        ]
        boundary_tie = sum(
            float(row["total_revenue"]) == boundary_value
            for row in ordered_rows
        ) > 1

    analysis = {
        "intent": "regional_product_revenue_ranking",
        "scope": "regional",
        "region": region,
        "metric": "total_revenue",
        "direction": direction,
        "requested_count": requested_count,
        "returned_count": len(selected_rows),
        "boundary_value": boundary_value,
        "boundary_tie": boundary_tie,
        "products": [
            {
                "product": row["product"],
                "total_revenue": float(row["total_revenue"]),
            }
            for row in selected_rows
        ],
    }
    return (
        {
            function_name: rows,
            "deterministic_analysis": analysis,
        },
        {region: rows},
    )


def _regional_product_comparison_intent(
    question: str,
) -> tuple[str, list[str] | None] | None:
    """Return (region, products) for explicit regional comparisons."""
    preserved = re.sub(r"[^a-zA-Z0-9\s]", " ", question)
    preserved = re.sub(r"\s+", " ", preserved).strip()
    match = re.fullmatch(
        r"compare\s+(.+?)\s+and\s+(.+?)\s+in\s+(.+)",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(3).strip(), [
            match.group(1).strip(),
            match.group(2).strip(),
        ]

    match = re.fullmatch(
        r"which\s+generates\s+more\s+revenue\s*,?\s*(.+?)\s+or\s+(.+?)"
        r"\s*,?\s+in\s+(.+)",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(3).strip(), [
            match.group(1).strip(),
            match.group(2).strip(),
        ]

    match = re.fullmatch(
        r"compare\s+the\s+top\s+two\s+products?\s+in\s+(.+)",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(), None
    return None


def _regional_rows_for_comparison(
    region: str,
    execution_cache: dict[str, Any],
) -> list[dict[str, Any]]:
    arguments = _parse_tool_arguments(
        "get_product_revenue_by_region",
        json.dumps({"region": region}),
    )
    cache_key = json.dumps(
        {
            "function": "get_product_revenue_by_region",
            "arguments": arguments,
        },
        sort_keys=True,
    )
    if cache_key not in execution_cache:
        execution_cache[cache_key] = ANALYTICS_FUNCTIONS[
            "get_product_revenue_by_region"
        ](**arguments)
    rows = execution_cache[cache_key]
    if not isinstance(rows, list):
        raise RuntimeError(
            "get_product_revenue_by_region returned an invalid result."
        )
    valid_rows = [
        row
        for row in rows
        if (
            isinstance(row, dict)
            and isinstance(row.get("product"), str)
            and isinstance(row.get("total_revenue"), (int, float))
        )
    ]
    if not valid_rows:
        raise RuntimeError(
            f"No product revenue data is available for {region}."
        )
    return valid_rows


def _deterministic_regional_product_comparison(
    region: str,
    products: list[str] | None,
    execution_cache: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Compare named products, or the resolved top two, in one region."""
    regional_rows: list[dict[str, Any]] | None = None
    if products is None:
        regional_rows = _regional_rows_for_comparison(
            region,
            execution_cache,
        )
        ranked_rows = sorted(
            regional_rows,
            key=lambda row: float(row["total_revenue"]),
            reverse=True,
        )
        if len(ranked_rows) < 2:
            raise RuntimeError(
                f"At least two products are required for comparison in {region}."
            )
        if float(ranked_rows[0]["total_revenue"]) == float(
            ranked_rows[1]["total_revenue"]
        ):
            raise RuntimeError(
                f"The top two products are tied in {region}; "
                "the comparison is ambiguous."
            )
        products = [ranked_rows[0]["product"], ranked_rows[1]["product"]]
    else:
        products = _parse_tool_arguments(
            "compare_products_by_region",
            json.dumps({"region": region, "products": products}),
        )["products"]

    comparison_arguments = _parse_tool_arguments(
        "compare_products_by_region",
        json.dumps({"region": region, "products": products}),
    )
    comparison_cache_key = json.dumps(
        {
            "function": "compare_products_by_region",
            "arguments": comparison_arguments,
        },
        sort_keys=True,
    )
    if comparison_cache_key not in execution_cache:
        execution_cache[comparison_cache_key] = ANALYTICS_FUNCTIONS[
            "compare_products_by_region"
        ](**comparison_arguments)
    comparison_rows = execution_cache[comparison_cache_key]
    if not isinstance(comparison_rows, list) or not comparison_rows:
        raise RuntimeError(
            f"No comparison data is available for {region}."
        )
    valid_comparison_rows = [
        row
        for row in comparison_rows
        if (
            isinstance(row, dict)
            and isinstance(row.get("product"), str)
            and isinstance(row.get("total_revenue"), (int, float))
        )
    ]
    if not valid_comparison_rows:
        raise RuntimeError(
            f"Comparison data is insufficient for {region}."
        )
    revenue_by_product = {
        row["product"]: float(row["total_revenue"])
        for row in valid_comparison_rows
    }
    missing_products = [
        product for product in products if product not in revenue_by_product
    ]
    if missing_products:
        raise RuntimeError(
            f"Comparison data is missing product(s): {', '.join(missing_products)}."
        )
    first_revenue = revenue_by_product[products[0]]
    second_revenue = revenue_by_product[products[1]]
    analysis = {
        "intent": "regional_product_revenue_comparison",
        "scope": "regional",
        "region": region,
        "metric": "total_revenue",
        "products": products,
        "revenues": {
            products[0]: first_revenue,
            products[1]: second_revenue,
        },
        "revenue_difference": first_revenue - second_revenue,
        "tie": first_revenue == second_revenue,
        "resolved_top_two": regional_rows is not None,
    }
    business_data: dict[str, Any] = {
        "compare_products_by_region": comparison_rows,
        "deterministic_analysis": analysis,
    }
    if regional_rows is not None:
        business_data["get_product_revenue_by_region"] = (
            execution_cache[
                json.dumps(
                    {
                        "function": "get_product_revenue_by_region",
                        "arguments": {"region": region},
                    },
                    sort_keys=True,
                )
            ]
        )
    return business_data, {region: comparison_rows}


def _revenue_percentage_intent(
    question: str,
) -> tuple[str, str, str | None, str | None] | None:
    """Return (kind, first_name, second_name, region) for explicit shares."""
    preserved = re.sub(r"[^a-zA-Z0-9\s]", " ", question)
    preserved = re.sub(r"\s+", " ", preserved).strip()
    if re.search(
        r"\b(quantity|units|profit|profitability|margin|customer|customers|"
        r"monthly|month)\b",
        preserved,
        flags=re.IGNORECASE,
    ):
        return None

    match = re.fullmatch(
        r"what\s+percentage\s+of\s+total\s+revenue\s+comes\s+from\s+"
        r"(?:the\s+)?region\s+(.+)",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return "region_share", match.group(1).strip(), None, None
    match = re.fullmatch(
        r"what\s+percentage\s+of\s+revenue\s+comes\s+from\s+(.+?)\s+region",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return "region_share", match.group(1).strip(), None, None

    match = re.fullmatch(
        r"what\s+percentage\s+of\s+total\s+revenue\s+comes\s+from\s+(.+)",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return "overall_product_share", match.group(1).strip(), None, None
    match = re.fullmatch(
        r"what\s+is\s+(.+?)\s+s\s+percentage\s+contribution\s+"
        r"to\s+total\s+revenue",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return "overall_product_share", match.group(1).strip(), None, None

    match = re.fullmatch(
        r"what\s+percentage\s+of\s+(.+?)\s+revenue\s+comes\s+from\s+(.+)",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return (
            "regional_product_share",
            match.group(2).strip(),
            None,
            match.group(1).strip(),
        )

    match = re.fullmatch(
        r"what\s+is\s+the\s+percentage\s+difference\s+between\s+(.+?)\s+"
        r"and\s+(.+?)\s+in\s+(.+)",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return (
            "regional_product_percentage_difference",
            match.group(1).strip(),
            match.group(2).strip(),
            match.group(3).strip(),
        )
    return None


def _rows_by_name(
    rows: Any,
    name_key: str,
    value_key: str,
    description: str,
) -> dict[str, float]:
    if not isinstance(rows, list):
        raise RuntimeError(f"{description} returned an invalid result.")
    result: dict[str, float] = {}
    for row in rows:
        if (
            isinstance(row, dict)
            and isinstance(row.get(name_key), str)
            and isinstance(row.get(value_key), (int, float))
        ):
            result[row[name_key]] = float(row[value_key])
    if not result:
        raise RuntimeError(f"No {description.lower()} data is available.")
    return result


def _cached_analytics_result(
    function_name: str,
    arguments: dict[str, Any],
    execution_cache: dict[str, Any],
) -> Any:
    cache_key = json.dumps(
        {"function": function_name, "arguments": arguments},
        sort_keys=True,
    )
    if cache_key not in execution_cache:
        execution_cache[cache_key] = ANALYTICS_FUNCTIONS[function_name](
            **arguments
        )
    return execution_cache[cache_key]


def _deterministic_revenue_percentage(
    kind: str,
    first_name: str,
    second_name: str | None,
    region: str | None,
    execution_cache: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Calculate an explicit revenue share or regional percentage difference."""
    if kind == "overall_product_share":
        product_rows = _cached_analytics_result(
            "get_product_revenue",
            {},
            execution_cache,
        )
        summary = _cached_analytics_result(
            "get_sales_summary",
            {},
            execution_cache,
        )
        products = _rows_by_name(
            product_rows,
            "product",
            "total_revenue",
            "overall product revenue",
        )
        if not isinstance(summary, dict) or not isinstance(
            summary.get("total_revenue"), (int, float)
        ):
            raise RuntimeError("Overall sales summary returned invalid revenue.")
        if first_name not in products:
            raise RuntimeError(f"Product is not present in overall revenue: {first_name}.")
        denominator = float(summary["total_revenue"])
        numerator = products[first_name]
        scope = "overall"
        business_data = {
            "get_product_revenue": product_rows,
            "get_sales_summary": summary,
        }
        regional_context: dict[str, list[dict[str, Any]]] = {}
    elif kind == "region_share":
        region_rows = _cached_analytics_result(
            "get_region_revenue",
            {},
            execution_cache,
        )
        summary = _cached_analytics_result(
            "get_sales_summary",
            {},
            execution_cache,
        )
        regions = _rows_by_name(
            region_rows,
            "region",
            "total_revenue",
            "region revenue",
        )
        if not isinstance(summary, dict) or not isinstance(
            summary.get("total_revenue"), (int, float)
        ):
            raise RuntimeError("Overall sales summary returned invalid revenue.")
        if first_name not in regions:
            raise RuntimeError(f"Region is not present in revenue data: {first_name}.")
        numerator = regions[first_name]
        denominator = float(summary["total_revenue"])
        scope = "overall"
        business_data = {
            "get_region_revenue": region_rows,
            "get_sales_summary": summary,
        }
        regional_context = {}
    elif kind == "regional_product_share":
        if region is None:
            raise RuntimeError("A named region is required for regional share.")
        product_rows = _cached_analytics_result(
            "get_product_revenue_by_region",
            _parse_tool_arguments(
                "get_product_revenue_by_region",
                json.dumps({"region": region}),
            ),
            execution_cache,
        )
        region_rows = _cached_analytics_result(
            "get_region_revenue",
            {},
            execution_cache,
        )
        products = _rows_by_name(
            product_rows,
            "product",
            "total_revenue",
            "regional product revenue",
        )
        regions = _rows_by_name(
            region_rows,
            "region",
            "total_revenue",
            "region revenue",
        )
        if first_name not in products:
            raise RuntimeError(
                f"Product is not present in regional revenue: {first_name}."
            )
        if region not in regions:
            raise RuntimeError(f"Region is not present in revenue data: {region}.")
        numerator = products[first_name]
        denominator = regions[region]
        scope = "regional"
        business_data = {
            "get_product_revenue_by_region": product_rows,
            "get_region_revenue": region_rows,
        }
        regional_context = {region: product_rows}
    else:
        if region is None or second_name is None:
            raise RuntimeError(
                "Two named products and a named region are required."
            )
        comparison_rows = _cached_analytics_result(
            "compare_products_by_region",
            _parse_tool_arguments(
                "compare_products_by_region",
                json.dumps(
                    {
                        "region": region,
                        "products": [first_name, second_name],
                    }
                ),
            ),
            execution_cache,
        )
        revenues = _rows_by_name(
            comparison_rows,
            "product",
            "total_revenue",
            "regional comparison",
        )
        missing = [
            product
            for product in (first_name, second_name)
            if product not in revenues
        ]
        if missing:
            raise RuntimeError(
                f"Comparison data is missing product(s): {', '.join(missing)}."
            )
        first_revenue = revenues[first_name]
        second_revenue = revenues[second_name]
        denominator = (first_revenue + second_revenue) / 2
        if denominator == 0:
            raise RuntimeError(
                "Percentage difference cannot be calculated with a zero denominator."
            )
        percentage_difference = (
            abs(first_revenue - second_revenue) / denominator * 100
        )
        business_data = {
            "compare_products_by_region": comparison_rows,
            "deterministic_analysis": {
                "intent": kind,
                "scope": "regional",
                "region": region,
                "metric": "total_revenue",
                "products": [first_name, second_name],
                "revenues": {
                    first_name: first_revenue,
                    second_name: second_revenue,
                },
                "formula": (
                    "abs(first_revenue - second_revenue) / "
                    "((first_revenue + second_revenue) / 2) * 100"
                ),
                "percentage_difference": percentage_difference,
                "percentage_point_difference": first_revenue - second_revenue,
                "tie": first_revenue == second_revenue,
            },
        }
        return business_data, {region: comparison_rows}

    if denominator == 0:
        raise RuntimeError("Revenue share cannot be calculated with zero revenue.")
    percentage = numerator / denominator * 100
    business_data["deterministic_analysis"] = {
        "intent": kind,
        "scope": scope,
        "region": region if scope == "regional" else None,
        "metric": "total_revenue",
        "entity": first_name,
        "numerator": numerator,
        "denominator": denominator,
        "formula": "numerator / denominator * 100",
        "percentage": percentage,
    }
    return business_data, regional_context


def _is_deterministic_recommendation_intent(question: str) -> bool:
    """Recognize explicit recommendation or data-insight requests."""
    normalized = question.lower()
    if re.search(
        r"\b(region|product|revenue|sales|business|data|insight|strategy|"
        r"recommend|focus|attention|improve)\b",
        normalized,
    ) is None:
        return False
    return bool(
        re.search(
            r"\b(recommend(?:ation|ations)?|suggest|should\s+the\s+business|"
            r"focus\s+on|needs?\s+attention|improve\s+revenue|business\s+"
            r"insights?|insights?\s+from)\b",
            normalized,
        )
    )


def _clean_extracted_region_name(raw: str) -> str:
    """Clean trailing comparison phrases from extracted region names."""
    cleaned = raw.strip()
    trailing_patterns = (
        r"\s+in\s+terms\s+of\s+revenue.*$",
        r"\s+in\s+revenue.*$",
        r"\s+by\s+revenue.*$",
        r"\s+for\s+revenue.*$",
        r"\s+sales.*$",
        r"\s+revenue.*$",
    )
    for pattern in trailing_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _regional_recommendation_comparison_intent(
    question: str,
) -> tuple[str, str] | None:
    """Recognize an explicit revenue comparison between two named regions."""
    preserved = re.sub(r"[^a-zA-Z0-9\s]", " ", question)
    preserved = re.sub(r"\s+", " ", preserved).strip()

    # 1. why is/does <r1> generating/generate more revenue than <r2>
    match = re.fullmatch(
        r"why\s+(?:is|does)\s+([a-zA-Z][a-zA-Z\s-]*?)\s+generat(?:ing|e)\s+more\s+"
        r"revenue\s+than\s+([a-zA-Z][a-zA-Z\s-]*?)(?:\s+and\s+(?:what|how|which|why)\b.*)?",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_extracted_region_name(match.group(1)), _clean_extracted_region_name(match.group(2))

    # 2. compare revenue between <r1> and <r2>
    match = re.fullmatch(
        r"compare\s+(?:the\s+)?revenue\s+between\s+([a-zA-Z][a-zA-Z\s-]*?)\s+and\s+"
        r"([a-zA-Z][a-zA-Z\s-]*?)(?:\s+and\s+(?:explain|what|how|which|why)\b.*)?",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_extracted_region_name(match.group(1)), _clean_extracted_region_name(match.group(2))

    # 3. compare <r1> and <r2> [in terms of revenue / in revenue]
    match = re.fullmatch(
        r"compare\s+([a-zA-Z][a-zA-Z\s-]*?)\s+and\s+([a-zA-Z][a-zA-Z\s-]*?)"
        r"(?:\s+(?:in\s+terms\s+of\s+revenue|in\s+revenue|by\s+revenue))?"
        r"(?:\s+and\s+(?:explain|what|how|which|why)\b.*)?",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_extracted_region_name(match.group(1)), _clean_extracted_region_name(match.group(2))

    # 4. how does <r1> perform compared with/to <r2>
    match = re.fullmatch(
        r"how\s+does\s+([a-zA-Z][a-zA-Z\s-]*?)\s+perform\s+compared\s+(?:with|to)\s+"
        r"([a-zA-Z][a-zA-Z\s-]*?)(?:\s+(?:in\s+terms\s+of\s+revenue|in\s+revenue|by\s+revenue))?"
        r"(?:\s+and\s+(?:explain|what|how|which|why)\b.*)?",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_extracted_region_name(match.group(1)), _clean_extracted_region_name(match.group(2))

    # 5. which/what sales channel generates more revenue in/for/between <r1> and/vs <r2>
    match = re.fullmatch(
        r"(?:which|what)\s+(?:sales\s+)?channel\s+generates?\s+more\s+revenue\s+"
        r"(?:in|for|between)\s+([a-zA-Z][a-zA-Z\s-]*?)\s+(?:and|vs\.?|versus)\s+"
        r"([a-zA-Z][a-zA-Z\s-]*?)(?:\s+and\s+(?:what|how|which|why)\b.*)?",
        preserved,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_extracted_region_name(match.group(1)), _clean_extracted_region_name(match.group(2))

    return None


def _deterministic_recommendation_evidence(
    execution_cache: dict[str, Any],
    regional_comparison: tuple[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build recommendation evidence from approved overall analytics rows."""
    function_names = (
        "get_sales_summary",
        "get_product_revenue",
        "get_region_revenue",
        "get_monthly_revenue",
    )
    results: dict[str, Any] = {}
    limitations: list[str] = []
    for function_name in function_names:
        try:
            results[function_name] = _cached_analytics_result(
                function_name,
                {},
                execution_cache,
            )
        except (RuntimeError, ValueError, TypeError) as error:
            logger.warning(
                "Deterministic recommendation evidence unavailable "
                "function=%s error_type=%s",
                function_name,
                type(error).__name__,
            )
            results[function_name] = []
            limitations.append(
                f"{function_name} was unavailable for this request."
            )

    summary = results["get_sales_summary"]
    product_rows = results["get_product_revenue"]
    region_rows = results["get_region_revenue"]
    month_rows = results["get_monthly_revenue"]

    valid_products = [
        row
        for row in product_rows
        if isinstance(row, dict)
        and isinstance(row.get("product"), str)
        and isinstance(row.get("total_revenue"), (int, float))
    ] if isinstance(product_rows, list) else []
    valid_regions = [
        row
        for row in region_rows
        if isinstance(row, dict)
        and isinstance(row.get("region"), str)
        and isinstance(row.get("total_revenue"), (int, float))
    ] if isinstance(region_rows, list) else []
    valid_months = [
        row
        for row in month_rows
        if isinstance(row, dict)
        and isinstance(row.get("order_month"), str)
        and isinstance(row.get("total_revenue"), (int, float))
    ] if isinstance(month_rows, list) else []

    if not valid_products:
        limitations.append("No complete overall product revenue ranking was available.")
    if not valid_regions:
        limitations.append("No complete overall region revenue ranking was available.")
    if not valid_months:
        limitations.append("No complete monthly revenue data was available.")
    if not isinstance(summary, dict) or not isinstance(
        summary.get("total_revenue"), (int, float)
    ):
        limitations.append("A valid overall revenue denominator was unavailable.")

    total_revenue = (
        float(summary["total_revenue"])
        if isinstance(summary, dict)
        and isinstance(summary.get("total_revenue"), (int, float))
        else None
    )
    product_ranking = [
        {
            "product": row["product"],
            "total_revenue": float(row["total_revenue"]),
        }
        for row in sorted(
            valid_products,
            key=lambda item: float(item["total_revenue"]),
            reverse=True,
        )
    ]
    region_ranking = [
        {
            "region": row["region"],
            "total_revenue": float(row["total_revenue"]),
        }
        for row in sorted(
            valid_regions,
            key=lambda item: float(item["total_revenue"]),
            reverse=True,
        )
    ]
    monthly_ranking = [
        {
            "order_month": row["order_month"],
            "total_revenue": float(row["total_revenue"]),
        }
        for row in sorted(
            valid_months,
            key=lambda item: float(item["total_revenue"]),
            reverse=True,
        )
    ]
    revenue_shares = []
    if total_revenue not in (None, 0):
        revenue_shares = [
            {
                "product": row["product"],
                "numerator": row["total_revenue"],
                "denominator": total_revenue,
                "percentage": row["total_revenue"] / total_revenue * 100,
            }
            for row in product_ranking
        ]
    elif total_revenue == 0:
        limitations.append(
            "Revenue shares could not be calculated because total revenue is zero."
        )

    quantity_available = False
    if isinstance(product_rows, list) and product_rows:
        quantity_available = all(
            isinstance(row, dict)
            and isinstance(row.get("total_quantity"), (int, float))
            for row in product_rows
        )
    if not quantity_available:
        limitations.append(
            "Quantity rankings were omitted because complete product quantity "
            "data was not returned."
        )

    evidence = {
        "intent": "evidence_based_business_recommendations",
        "scope": "overall",
        "observed_evidence": {
            "product_revenue_ranking": product_ranking,
            "region_revenue_ranking": region_ranking,
            "monthly_revenue_ranking": monthly_ranking,
            "revenue_shares": revenue_shares,
        },
        "verified_calculations": {
            "total_revenue": total_revenue,
            "revenue_share_formula": "product_revenue / total_revenue * 100",
            "quantity_ranking_included": quantity_available,
        },
        "recommendation_limits": [
            "Revenue rankings do not establish profitability or customer preference.",
            "Revenue data does not establish causes, marketing effectiveness, or price elasticity.",
            "Product rows do not establish basket co-purchase, bundling, or cross-selling.",
            "Actions should be framed as investigations unless the supplied metric directly supports them.",
        ],
        "limitations": limitations,
    }

    if regional_comparison is not None:
        higher_region, comparison_region = regional_comparison
        region_rows_by_name = {
            row["region"]: float(row["total_revenue"])
            for row in valid_regions
        }
        regional_products: dict[str, Any] = {}
        for region in (higher_region, comparison_region):
            try:
                region_product_rows = _cached_analytics_result(
                    "get_product_revenue_by_region",
                    _parse_tool_arguments(
                        "get_product_revenue_by_region",
                        json.dumps({"region": region}),
                    ),
                    execution_cache,
                )
            except (RuntimeError, ValueError, TypeError) as error:
                logger.warning(
                    "Regional recommendation evidence unavailable "
                    "region=%s error_type=%s",
                    region,
                    type(error).__name__,
                )
                region_product_rows = []
                limitations.append(
                    f"Regional product data for {region} was unavailable."
                )
            regional_products[region] = region_product_rows
        results["regional_recommendation_product_rows"] = regional_products

        valid_regional_products: dict[str, list[dict[str, Any]]] = {}
        for region, rows in regional_products.items():
            valid_rows = [
                row
                for row in rows
                if (
                    isinstance(row, dict)
                    and isinstance(row.get("product"), str)
                    and isinstance(row.get("total_revenue"), (int, float))
                )
            ] if isinstance(rows, list) else []
            valid_regional_products[region] = valid_rows
            if not valid_rows:
                limitations.append(
                    f"No complete regional product revenue data was available for {region}."
                )

        higher_revenue = region_rows_by_name.get(higher_region)
        comparison_revenue = region_rows_by_name.get(comparison_region)
        if higher_revenue is None or comparison_revenue is None:
            limitations.append(
                "The requested regions were not both present in the region revenue data."
            )
        regional_quantity_evidence = {}
        regional_product_revenue_shares: dict[str, list[dict[str, Any]]] = {}
        regional_totals: dict[str, dict[str, float | int]] = {}
        for region, rows in valid_regional_products.items():
            if rows and all(
                isinstance(row.get("total_quantity"), (int, float))
                for row in rows
            ):
                regional_quantity_evidence[region] = [
                    {
                        "product": row["product"],
                        "total_revenue": float(row["total_revenue"]),
                        "total_quantity": float(row["total_quantity"]),
                        "total_transactions": (
                            float(row["total_transactions"])
                            if isinstance(row.get("total_transactions"), (int, float))
                            else None
                        ),
                    }
                    for row in rows
                ]
            else:
                limitations.append(
                    f"Complete regional quantity data was unavailable for {region}."
                )
            if rows:
                region_revenue = region_rows_by_name.get(region)
                if region_revenue not in (None, 0):
                    regional_product_revenue_shares[region] = [
                        {
                            "product": row["product"],
                            "numerator": float(row["total_revenue"]),
                            "denominator": region_revenue,
                            "percentage": round(
                                float(row["total_revenue"])
                                / region_revenue
                                * 100,
                                14,
                            ),
                        }
                        for row in rows
                    ]
                complete_transactions = all(
                    isinstance(row.get("total_transactions"), (int, float))
                    for row in rows
                )
                complete_quantity = all(
                    isinstance(row.get("total_quantity"), (int, float))
                    for row in rows
                )
                if complete_transactions and complete_quantity:
                    regional_totals[region] = {
                        "total_revenue": region_revenue,
                        "total_quantity": sum(
                            float(row["total_quantity"]) for row in rows
                        ),
                        "total_transactions": sum(
                            int(row["total_transactions"]) for row in rows
                        ),
                    }

        # ── Sales-channel breakdown for both compared regions ──────────────
        # sales_channel already exists in the dataset; fetch and expose it
        # so the LLM does not recommend collecting data that is already present.
        channel_breakdown: dict[str, list[dict[str, Any]]] = {}
        channel_limitations: list[str] = []
        compared_regions_for_channel = [
            r for r in (higher_region, comparison_region) if r
        ]
        if compared_regions_for_channel:
            try:
                channel_breakdown = get_regional_channel_summary(
                    compared_regions_for_channel
                )
            except (RuntimeError, ValueError, TypeError) as error:
                logger.warning(
                    "Regional channel summary unavailable "
                    "regions=%s error_type=%s",
                    compared_regions_for_channel,
                    type(error).__name__,
                )
                channel_limitations.append(
                    "Sales-channel breakdown was unavailable for this request."
                )
        if channel_limitations:
            limitations.extend(channel_limitations)

        evidence["scope"] = "regional_comparison"
        regional_shares = {}
        if total_revenue not in (None, 0):
            if higher_revenue is not None:
                regional_shares[higher_region] = round(
                    higher_revenue / total_revenue * 100,
                    14,
                )
            if comparison_revenue is not None:
                regional_shares[comparison_region] = round(
                    comparison_revenue / total_revenue * 100,
                    14,
                )
        elif total_revenue == 0:
            limitations.append(
                "Regional revenue shares could not be calculated because "
                "total revenue is zero."
            )
        channel_revenue_advantages: dict[str, dict[str, Any]] = {}
        for reg_name, ch_rows in channel_breakdown.items():
            ret_entry = next((r for r in ch_rows if str(r.get("sales_channel")).lower() == "retail"), None)
            onl_entry = next((r for r in ch_rows if str(r.get("sales_channel")).lower() == "online"), None)
            if ret_entry and onl_entry:
                ret_rev = float(ret_entry["total_revenue"])
                onl_rev = float(onl_entry["total_revenue"])
                diff = ret_rev - onl_rev
                pct = round((diff / onl_rev) * 100, 2) if onl_rev != 0 else None
                channel_revenue_advantages[reg_name] = {
                    "channel_compared": "Retail",
                    "comparison_channel_denominator": "Online",
                    "retail_revenue": ret_rev,
                    "online_revenue": onl_rev,
                    "revenue_difference": diff,
                    "retail_revenue_advantage_percentage": pct,
                    "formula": "((retail_revenue - online_revenue) / online_revenue) * 100",
                }

        evidence["verified_calculations"] = {
            **evidence["verified_calculations"],
            "regional_revenue_share_formula": (
                "region_revenue / total_revenue * 100"
            ),
            "regional_revenue_shares": regional_shares,
            "regional_revenue_share_denominator": total_revenue,
            "channel_revenue_advantage_formula": (
                "((channel_revenue - comparison_channel_revenue) / comparison_channel_revenue) * 100"
            ),
            "channel_revenue_advantages": channel_revenue_advantages,
        }
        evidence["regional_comparison"] = {
            "higher_revenue_region": higher_region,
            "comparison_region": comparison_region,
            "higher_region_revenue": higher_revenue,
            "comparison_region_revenue": comparison_revenue,
            "revenue_difference": (
                higher_revenue - comparison_revenue
                if higher_revenue is not None and comparison_revenue is not None
                else None
            ),
            "revenue_shares": regional_shares,
            "regional_totals": regional_totals,
            "regional_product_revenue_shares": (
                regional_product_revenue_shares
            ),
            "product_revenue_rows": {
                region: [
                    {
                        "product": row["product"],
                        "total_revenue": float(row["total_revenue"]),
                    }
                    for row in rows
                ]
                for region, rows in valid_regional_products.items()
            },
            "product_quantity_rows": regional_quantity_evidence,
            # Per-channel breakdown (revenue / quantity / transactions) for each
            # compared region.  Keys are region names; each value is a list of
            # {sales_channel, total_revenue, total_quantity, total_transactions}.
            "channel_breakdown": channel_breakdown,
            "channel_breakdown_available": bool(channel_breakdown),
            "channel_revenue_advantages": channel_revenue_advantages,
            "cost_of_goods_sold_available": False,
            "cost_data_available": False,
            "available_product_data_fields": [
                "product",
                "total_revenue",
                "total_quantity",
                "total_transactions",
            ],
            "available_dataset_fields": [
                "transaction_id",
                "product",
                "region",
                "sales_channel",
                "quantity",
                "unit_price",
                "discount_amount",
                "revenue",
            ],
            "basket_analysis_note": (
                "Each transaction_id in this dataset contains exactly one product; "
                "multi-product basket composition cannot be calculated from these rows. "
                "Do not report or recommend basket co-purchase metrics."
            ),
            "product_data_complete": {
                region: bool(rows)
                and all(
                    isinstance(row.get("total_quantity"), (int, float))
                    and isinstance(row.get("total_transactions"), (int, float))
                    for row in rows
                )
                for region, rows in valid_regional_products.items()
            },
            "grounding_rules": [
                "Use the supplied regional totals and counts verbatim.",
                "Regional product shares use the matching regional revenue denominator.",
                "Do not infer profitability, margins, customer behavior, or causality.",
                "Do not recommend collecting fields already present in the dataset: "
                "transaction_id, product, region, sales_channel, quantity, "
                "unit_price, discount_amount, revenue are all available.",
                "Use the supplied channel_breakdown rows for sales-channel analysis; "
                "do not recommend collecting sales-channel data.",
                "For sales-channel comparisons, use the terminology 'revenue difference' "
                "or 'revenue advantage'; never use 'profit margin', 'profitability', or "
                "'channel effectiveness' when only revenue data is available. "
                "Do not equate higher revenue with higher profitability or channel effectiveness.",
                "Always calculate percentage differences using the comparison channel as "
                "the denominator: ((channel_revenue - comparison_channel_revenue) / "
                "comparison_channel_revenue) * 100. For instance, South Retail revenue advantage "
                "over Online is 6.58% and North Retail revenue advantage over Online is 56.30%.",
                "Recommendations may analyze average selling prices and discounts by channel "
                "and region using available unit_price, discount_amount, sales_channel, and region.",
                "Profitability and margin claims require cost data which is not present: "
                "Cost of goods sold (COGS) and operational cost data are completely unavailable; "
                "explicitly identify cost of goods sold as unavailable and do not calculate, infer, "
                "or claim profitability without cost data.",
                "Distinguish evidence-backed observations from proposed experiments: "
                "present promotional, discounting, and pricing initiatives as experiments "
                "whose outcomes must be measured, not guaranteed improvements.",
                "Basket co-purchase analysis is not possible: each transaction contains "
                "one product only. Do not suggest or report multi-product basket metrics.",
                "Customer segment data is not available; it may be suggested as "
                "additional data to collect but must not be invented.",
            ],
            "monthly_data_scope": "overall_only_not_region_specific",
        }
        evidence["recommendation_limits"].extend(
            [
                "Regional product revenue and quantity rows are evidence of observed "
                "regional differences, not proof of their causes.",
                "Overall monthly totals cannot explain or establish regional peaks.",
                "Do not recommend collecting product-level revenue, quantity, "
                "transaction, or sales-channel data: all of these fields exist in "
                "the dataset and channel_breakdown rows are already supplied.",
                "If regional product and channel rows do not explain the difference, "
                "collect transaction-level customer, pricing, cost, and marketing "
                "data before recommending an intervention.",
                "Do not claim basket co-purchase, bundling, or cross-selling: "
                "each transaction contains one product and basket data is absent.",
                "Do not claim profitability or margins without cost data.",
                "Cost of goods sold (COGS) is unavailable; do not calculate or infer profit margins.",
                "Promotions and discounts must be framed as measurable experiments, not guaranteed improvements.",
            ]
        )
        evidence["scope"] = "regional_comparison"

    return evidence, results


def _deterministic_second_region_analysis(
    execution_cache: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Resolve the second revenue region and its top product from live data."""
    region_function = ANALYTICS_FUNCTIONS["get_region_revenue"]
    region_cache_key = json.dumps(
        {"function": "get_region_revenue", "arguments": {}},
        sort_keys=True,
    )
    if region_cache_key not in execution_cache:
        execution_cache[region_cache_key] = region_function()
    region_rows = execution_cache[region_cache_key]
    if not isinstance(region_rows, list) or len(region_rows) < 2:
        raise RuntimeError(
            "Unable to determine the second-highest region from available data."
        )

    normalized_regions = [
        row
        for row in region_rows
        if isinstance(row, dict)
        and isinstance(row.get("region"), str)
        and isinstance(row.get("total_revenue"), (int, float))
    ]
    if len(normalized_regions) < 2:
        raise RuntimeError(
            "Region revenue data is insufficient for a second-highest ranking."
        )
    ranked_regions = sorted(
        normalized_regions,
        key=lambda row: float(row["total_revenue"]),
        reverse=True,
    )
    second_region = ranked_regions[1]
    if (
        float(second_region["total_revenue"])
        == float(ranked_regions[0]["total_revenue"])
        or (
            len(ranked_regions) > 2
            and float(second_region["total_revenue"])
            == float(ranked_regions[2]["total_revenue"])
        )
    ):
        raise RuntimeError(
            "The second-highest region is ambiguous because of a revenue tie."
        )

    arguments = _parse_tool_arguments(
        "get_product_revenue_by_region",
        json.dumps({"region": second_region["region"]}),
    )
    product_function = ANALYTICS_FUNCTIONS["get_product_revenue_by_region"]
    product_cache_key = json.dumps(
        {"function": "get_product_revenue_by_region", "arguments": arguments},
        sort_keys=True,
    )
    if product_cache_key not in execution_cache:
        execution_cache[product_cache_key] = product_function(**arguments)
    product_rows = execution_cache[product_cache_key]
    if not isinstance(product_rows, list) or not product_rows:
        raise RuntimeError(
            f"No product revenue data is available for {second_region['region']}."
        )
    valid_products = [
        row
        for row in product_rows
        if isinstance(row, dict)
        and isinstance(row.get("product"), str)
        and isinstance(row.get("total_revenue"), (int, float))
    ]
    if not valid_products:
        raise RuntimeError(
            f"Product revenue data is insufficient for {second_region['region']}."
        )
    ranked_products = sorted(
        valid_products,
        key=lambda row: float(row["total_revenue"]),
        reverse=True,
    )
    best_product = ranked_products[0]
    if len(ranked_products) > 1 and float(best_product["total_revenue"]) == float(
        ranked_products[1]["total_revenue"]
    ):
        raise RuntimeError(
            f"The best-performing product is ambiguous in {second_region['region']}."
        )

    analysis = {
        "intent": "second_highest_region_best_product",
        "selected_region": second_region["region"],
        "selected_region_revenue": float(second_region["total_revenue"]),
        "best_product": {
            "product": best_product["product"],
            "total_revenue": float(best_product["total_revenue"]),
        },
    }
    return (
        {
            "get_region_revenue": region_rows,
            "get_product_revenue_by_region": product_rows,
            "deterministic_analysis": analysis,
        },
        {second_region["region"]: product_rows},
    )


def _build_router_messages(
    question: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build a bounded router context without truncating the current question."""
    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("Question cannot be empty.")

    history = [
        _compact_router_history_message(
            {"role": message["role"], "content": message["content"].strip()}
        )
        for message in (conversation_history or [])
        if (
            isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
            and message["content"].strip()
            and not (
                message.get("role") == "assistant"
                and message["content"].strip().lower().startswith(
                    ROUTER_FAILURE_PREFIXES
                )
            )
        )
    ]
    if history and history[-1]["role"] == "user" and (
        history[-1]["content"] == normalized_question
    ):
        history.pop()

    base_messages = [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": normalized_question},
    ]
    base_size = sum(len(message["content"]) for message in base_messages)
    if base_size > MAX_ROUTER_PROMPT_CHARS:
        raise ValueError("Question is too long for the AI context limit.")

    selected_history: list[dict[str, str]] = []
    available_chars = MAX_ROUTER_PROMPT_CHARS - base_size
    for message in reversed(history):
        message_size = len(message["content"])
        if message_size > available_chars:
            break
        selected_history.append(message)
        available_chars -= message_size
    selected_history.reverse()
    omitted_count = len(history) - len(selected_history)
    if omitted_count:
        logger.info(
            "Conversation history bounded omitted_messages=%s retained_messages=%s",
            omitted_count,
            len(selected_history),
        )
    return [
        base_messages[0],
        *selected_history,
        base_messages[1],
    ]


def _tool_call_cache_key(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function", {})
    arguments = function.get("arguments", "{}")
    try:
        normalized_arguments = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        normalized_arguments = arguments
    return json.dumps(
        {
            "function": function.get("name"),
            "arguments": normalized_arguments,
        },
        sort_keys=True,
    )


def _remove_duplicate_tool_exchanges(
    messages: list[dict[str, Any]],
    duplicate_keys: set[str],
) -> int:
    """Remove only complete prior exchanges whose calls are all duplicates."""
    removed = 0
    index = len(messages) - 1
    while index >= 0:
        message = messages[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            index -= 1
            continue
        tool_calls = message["tool_calls"]
        exchange_keys = {_tool_call_cache_key(call) for call in tool_calls}
        if not exchange_keys or not exchange_keys.issubset(duplicate_keys):
            index -= 1
            continue
        tool_call_ids = {
            call.get("id")
            for call in tool_calls
            if call.get("id")
        }
        end = index + 1
        while (
            end < len(messages)
            and messages[end].get("role") == "tool"
            and messages[end].get("tool_call_id") in tool_call_ids
        ):
            end += 1
        del messages[index:end]
        removed += end - index
        index -= 1
    return removed


def _compact_router_history_message(
    message: dict[str, str],
) -> dict[str, str]:
    """Bound prior assistant prose while preserving context for references."""
    if (
        message["role"] != "assistant"
        or len(message["content"]) <= MAX_ROUTER_ASSISTANT_HISTORY_CHARS
    ):
        return message
    prefix_length = len(ROUTER_ASSISTANT_HISTORY_PREFIX)
    separator = " [...] "
    available = MAX_ROUTER_ASSISTANT_HISTORY_CHARS - prefix_length - len(separator)
    first_length = max(1, int(available * 0.6))
    last_length = max(1, available - first_length)
    content = (
        ROUTER_ASSISTANT_HISTORY_PREFIX
        + message["content"][:first_length].rstrip()
        + separator
        + message["content"][-last_length:].lstrip()
    )
    return {"role": "assistant", "content": content}


def answer_question(
    question: str,
    conversation_history: list[dict[str, str]] | None = None,
    conversation_id: str | None = None,
) -> tuple[str, dict[str, float], dict[str, Any]]:
    messages = _build_router_messages(question, conversation_history)
    planning_ms = 0.0
    database_ms = 0.0
    response_generation_ms = 0.0
    business_data: dict[str, Any] = {}
    regional_context: dict[str, list[dict[str, Any]]] = {}
    execution_cache: dict[str, Any] = {}
    completed_tool_call_keys: set[str] = set()
    final_message: Any | None = None
    router_retry_used = False
    planning_tools = _select_planning_tools(
        question,
        conversation_history,
    )

    if _is_second_region_best_product_intent(question):
        deterministic_started = time.perf_counter()
        deterministic_data, deterministic_regional_context = (
            _deterministic_second_region_analysis(execution_cache)
        )
        database_ms += (time.perf_counter() - deterministic_started) * 1000
        response_started = time.perf_counter()
        final_response = _chat_completion(
            _final_answer_messages(
                question,
                deterministic_data,
                deterministic_regional_context,
            ),
            stage="final_answer_deterministic_second_region",
            round_number=1,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=1024,
        )
        response_generation_ms += (time.perf_counter() - response_started) * 1000
        final_message = (final_response.choices[0].message.content or "").strip()
        if not final_message:
            finish_reason = getattr(
                final_response.choices[0], "finish_reason", None
            )
            raise RuntimeError(
                "AI returned an empty grounded answer "
                f"(finish_reason={finish_reason})."
            )
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": round(response_generation_ms, 2),
            "total_ms": round(database_ms + response_generation_ms, 2),
        }
        return final_message, timings, deterministic_data

    regional_recommendation_comparison = (
        _regional_recommendation_comparison_intent(question)
    )
    if (
        regional_recommendation_comparison is not None
        or _is_deterministic_recommendation_intent(question)
    ):
        deterministic_started = time.perf_counter()
        evidence, recommendation_results = (
            _deterministic_recommendation_evidence(
                execution_cache,
                regional_recommendation_comparison,
            )
        )
        deterministic_data = dict(recommendation_results)
        deterministic_data["deterministic_analysis"] = evidence
        database_ms = (time.perf_counter() - deterministic_started) * 1000

        # If user did not ask for recommendations/strategies, answer purely deterministically without Groq
        if not _is_deterministic_recommendation_intent(question):
            final_message = _format_offline_regional_comparison(question, evidence)
            timings = {
                "planning_ms": 0.0,
                "database_ms": round(database_ms, 2),
                "response_generation_ms": 0.0,
                "total_ms": round(database_ms, 2),
            }
            return final_message, timings, deterministic_data

        response_started = time.perf_counter()
        final_response = _chat_completion(
            _final_answer_messages(question, deterministic_data),
            stage="final_answer_deterministic_recommendation",
            round_number=1,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=1024,
        )
        response_generation_ms = (
            time.perf_counter() - response_started
        ) * 1000
        final_message = (final_response.choices[0].message.content or "").strip()
        if not final_message:
            finish_reason = getattr(
                final_response.choices[0], "finish_reason", None
            )
            raise RuntimeError(
                "AI returned an empty grounded answer "
                f"(finish_reason={finish_reason})."
            )
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": round(
                response_generation_ms,
                2,
            ),
            "total_ms": round(
                database_ms + response_generation_ms,
                2,
            ),
        }
        return final_message, timings, deterministic_data

    revenue_percentage_intent = _revenue_percentage_intent(question)
    if revenue_percentage_intent is not None:
        (
            percentage_kind,
            first_name,
            second_name,
            region,
        ) = revenue_percentage_intent
        deterministic_started = time.perf_counter()
        (
            deterministic_data,
            deterministic_regional_context,
        ) = _deterministic_revenue_percentage(
            percentage_kind,
            first_name,
            second_name,
            region,
            execution_cache,
        )
        database_ms = (time.perf_counter() - deterministic_started) * 1000
        response_started = time.perf_counter()
        final_response = _chat_completion(
            _final_answer_messages(
                question,
                deterministic_data,
                deterministic_regional_context,
            ),
            stage="final_answer_deterministic_revenue_percentage",
            round_number=1,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=1024,
        )
        response_generation_ms = (
            time.perf_counter() - response_started
        ) * 1000
        final_message = (final_response.choices[0].message.content or "").strip()
        if not final_message:
            finish_reason = getattr(
                final_response.choices[0], "finish_reason", None
            )
            raise RuntimeError(
                "AI returned an empty grounded answer "
                f"(finish_reason={finish_reason})."
            )
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": round(
                response_generation_ms,
                2,
            ),
            "total_ms": round(
                database_ms + response_generation_ms,
                2,
            ),
        }
        return final_message, timings, deterministic_data

    regional_product_comparison_intent = _regional_product_comparison_intent(
        question
    )
    if regional_product_comparison_intent is not None:
        region, products = regional_product_comparison_intent
        deterministic_started = time.perf_counter()
        (
            deterministic_data,
            deterministic_regional_context,
        ) = _deterministic_regional_product_comparison(
            region,
            products,
            execution_cache,
        )
        database_ms = (time.perf_counter() - deterministic_started) * 1000
        response_started = time.perf_counter()
        final_response = _chat_completion(
            _final_answer_messages(
                question,
                deterministic_data,
                deterministic_regional_context,
            ),
            stage="final_answer_deterministic_regional_product_comparison",
            round_number=1,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=1024,
        )
        response_generation_ms = (
            time.perf_counter() - response_started
        ) * 1000
        final_message = (final_response.choices[0].message.content or "").strip()
        if not final_message:
            finish_reason = getattr(
                final_response.choices[0], "finish_reason", None
            )
            raise RuntimeError(
                "AI returned an empty grounded answer "
                f"(finish_reason={finish_reason})."
            )
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": round(
                response_generation_ms,
                2,
            ),
            "total_ms": round(
                database_ms + response_generation_ms,
                2,
            ),
        }
        return final_message, timings, deterministic_data

    regional_product_ranking_intent = _regional_product_ranking_intent(question)
    if regional_product_ranking_intent is not None:
        region, direction, requested_count = regional_product_ranking_intent
        deterministic_started = time.perf_counter()
        (
            deterministic_data,
            deterministic_regional_context,
        ) = _deterministic_regional_product_ranking(
            region,
            direction,
            requested_count,
            execution_cache,
        )
        database_ms = (time.perf_counter() - deterministic_started) * 1000
        response_started = time.perf_counter()
        final_response = _chat_completion(
            _final_answer_messages(
                question,
                deterministic_data,
                deterministic_regional_context,
            ),
            stage="final_answer_deterministic_regional_product_ranking",
            round_number=1,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=1024,
        )
        response_generation_ms = (
            time.perf_counter() - response_started
        ) * 1000
        final_message = (final_response.choices[0].message.content or "").strip()
        if not final_message:
            finish_reason = getattr(
                final_response.choices[0], "finish_reason", None
            )
            raise RuntimeError(
                "AI returned an empty grounded answer "
                f"(finish_reason={finish_reason})."
            )
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": round(
                response_generation_ms,
                2,
            ),
            "total_ms": round(
                database_ms + response_generation_ms,
                2,
            ),
        }
        return final_message, timings, deterministic_data

    product_ranking_intent = _product_ranking_intent(question)
    if product_ranking_intent is not None:
        direction, requested_count, comparison = product_ranking_intent
        deterministic_started = time.perf_counter()
        deterministic_data = _deterministic_product_ranking(
            direction,
            requested_count,
            comparison,
            execution_cache,
        )
        database_ms = (time.perf_counter() - deterministic_started) * 1000
        response_started = time.perf_counter()
        final_response = _chat_completion(
            _final_answer_messages(question, deterministic_data),
            stage="final_answer_deterministic_product_ranking",
            round_number=1,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=1024,
        )
        response_generation_ms = (
            time.perf_counter() - response_started
        ) * 1000
        final_message = (final_response.choices[0].message.content or "").strip()
        if not final_message:
            finish_reason = getattr(
                final_response.choices[0], "finish_reason", None
            )
            raise RuntimeError(
                "AI returned an empty grounded answer "
                f"(finish_reason={finish_reason})."
            )
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": round(response_generation_ms, 2),
            "total_ms": round(
                database_ms + response_generation_ms,
                2,
            ),
        }
        return final_message, timings, deterministic_data

    revenue_extreme_intent = _revenue_extreme_intent(question)
    if revenue_extreme_intent is not None:
        dataset, direction = revenue_extreme_intent
        deterministic_started = time.perf_counter()
        deterministic_data = _deterministic_revenue_extreme(
            dataset,
            direction,
            execution_cache,
        )
        database_ms = (time.perf_counter() - deterministic_started) * 1000
        final_message = _format_offline_revenue_extreme(
            dataset,
            direction,
            deterministic_data,
        )
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": 0.0,
            "total_ms": round(database_ms, 2),
        }
        return final_message, timings, deterministic_data

    if _is_overall_summary_intent(question):
        deterministic_started = time.perf_counter()
        deterministic_data = _deterministic_overall_summary(execution_cache)
        database_ms = (time.perf_counter() - deterministic_started) * 1000
        summary_payload = deterministic_data.get("get_sales_summary", {})
        final_message = _format_offline_overall_summary(summary_payload)
        timings = {
            "planning_ms": 0.0,
            "database_ms": round(database_ms, 2),
            "response_generation_ms": 0.0,
            "total_ms": round(database_ms, 2),
        }
        return final_message, timings, deterministic_data

    for _round in range(MAX_TOOL_ROUNDS):
        planning_started = time.perf_counter()
        planning_response = _chat_completion(
            messages,
            stage="planning",
            round_number=_round + 1,
            tools=planning_tools,
            tool_choice="auto",
            temperature=0,
            max_tokens=128,
        )
        planning_ms += (time.perf_counter() - planning_started) * 1000
        message = planning_response.choices[0].message
        tool_calls = message.tool_calls or []
        if not tool_calls:
            answer = (message.content or "").strip()
            if not answer and not business_data:
                normalized_question = question.strip().lower()
                database_question = bool(
                    re.search(
                        r"\b(revenue|sales|product|products|region|regions|"
                        r"quantity|transaction|transactions|monthly|month|"
                        r"analytics|business data|compare|highest|lowest|"
                        r"top|performance|performs|recommend|strategy|"
                        r"strategies|summary)\b",
                        normalized_question,
                    )
                )
                if database_question and not router_retry_used:
                    router_retry_used = True
                    logger.info(
                        "Empty analytics router response; retrying round=%s",
                        _round + 1,
                    )
                    continue
                if database_question:
                    raise RuntimeError(
                        "AI could not route the analytics question after one retry."
                    )
                raise RuntimeError("AI returned an empty conversational answer.")
            if not business_data:
                final_message = answer
                break

            response_started = time.perf_counter()
            final_response = _chat_completion(
                _final_answer_messages(question, business_data, regional_context),
                stage="final_answer",
                round_number=_round + 1,
                temperature=0,
                reasoning_effort="low",
                max_completion_tokens=1024,
            )
            response_generation_ms += (
                time.perf_counter() - response_started
            ) * 1000
            final_message = (
                final_response.choices[0].message.content or ""
            ).strip()
            if not final_message:
                finish_reason = getattr(
                    final_response.choices[0], "finish_reason", None
                )
                raise RuntimeError(
                    "AI returned an empty grounded answer "
                    f"(finish_reason={finish_reason})."
                )
            break

        assistant_message = message.model_dump(exclude_none=True)
        validated_calls: list[tuple[Any, str, dict[str, Any], str]] = []
        duplicate_keys: set[str] = set()
        for tool_call in tool_calls:
            function_name = tool_call.function.name
            function = ANALYTICS_FUNCTIONS.get(function_name)
            if function is None:
                raise RuntimeError(
                    f"AI requested an unapproved analytics function: {function_name}"
                )
            arguments = _parse_tool_arguments(
                function_name, tool_call.function.arguments
            )
            cache_key = json.dumps(
                {"function": function_name, "arguments": arguments},
                sort_keys=True,
            )
            validated_calls.append(
                (tool_call, function_name, arguments, cache_key)
            )
            if cache_key in completed_tool_call_keys:
                duplicate_keys.add(
                    _tool_call_cache_key(
                        {
                            "id": tool_call.id,
                            "function": {
                                "name": function_name,
                                "arguments": json.dumps(
                                    arguments,
                                    sort_keys=True,
                                ),
                            },
                        }
                    )
                )
        if duplicate_keys:
            removed_messages = _remove_duplicate_tool_exchanges(
                messages,
                duplicate_keys,
            )
            if removed_messages:
                logger.info(
                    "Compacted duplicate tool exchange messages=%s",
                    removed_messages,
                )
        messages.append(assistant_message)
        database_started = time.perf_counter()
        for tool_call, function_name, arguments, cache_key in validated_calls:
            if cache_key not in execution_cache:
                try:
                    execution_cache[cache_key] = function(**arguments)
                except Exception as error:
                    raise RuntimeError(
                        f"Analytics function failed: {function_name}"
                    ) from error
            result = execution_cache[cache_key]
            if function_name in {
                "get_product_revenue_by_region",
                "compare_products_by_region",
            }:
                region = arguments["region"]
                if isinstance(result, list) and (
                    region not in regional_context
                    or len(result) > len(regional_context[region])
                ):
                    regional_context[region] = result
            elif function_name == "get_region_product_performance":
                region = arguments["region"]
                rows = result.get("products", []) if isinstance(result, dict) else []
                if len(rows) > len(regional_context.get(region, [])):
                    regional_context[region] = rows
            if function_name not in business_data:
                business_data[function_name] = result
            elif business_data[function_name] != result:
                existing = business_data[function_name]
                if not isinstance(existing, list):
                    existing = [existing]
                existing.append(result)
                business_data[function_name] = existing
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": function_name,
                    "content": json.dumps(result, default=str),
                }
            )
            completed_tool_call_keys.add(cache_key)
        database_ms += (time.perf_counter() - database_started) * 1000

    if final_message is None:
        if not business_data:
            raise RuntimeError(
                "AI could not produce a grounded analytics answer."
            )
        response_started = time.perf_counter()
        final_response = _chat_completion(
            _final_answer_messages(question, business_data, regional_context),
            stage="final_answer_after_max_rounds",
            round_number=MAX_TOOL_ROUNDS,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=1024,
        )
        response_generation_ms = (
            time.perf_counter() - response_started
        ) * 1000
        final_message = (final_response.choices[0].message.content or "").strip()
        if not final_message:
            finish_reason = getattr(
                final_response.choices[0], "finish_reason", None
            )
            raise RuntimeError(
                "AI returned an empty grounded answer "
                f"(finish_reason={finish_reason})."
            )

    timings = {
        "planning_ms": round(planning_ms, 2),
        "database_ms": round(database_ms, 2),
        "response_generation_ms": round(response_generation_ms, 2),
        "total_ms": round(
            planning_ms + database_ms + response_generation_ms, 2
        ),
    }
    return final_message, timings, business_data
