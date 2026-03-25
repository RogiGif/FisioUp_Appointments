from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum, F
from django.db.models.functions import Coalesce
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponseForbidden
from django.urls import reverse
from django.utils import timezone

from core.decorators import backoffice_required
from core.permissions import can_access_backoffice, is_admin_role
from core.services.audit import log_audit_event, snapshot_instance
from core.forms import ProductForm, StockEntryForm, StockAdjustmentForm, ProductCategoryForm
from core.models import Product, ProductCategory, StockMovement
from core.utils.stock import get_stock, get_default_location
from core.views.common import apply_terms_filter


PRODUCT_AUDIT_FIELDS = [
    "id",
    "name",
    "sku",
    "category_id",
    "is_active",
    "unit_base",
    "min_stock_alert",
    "unit_per_pack",
    "notes",
]
PRODUCT_CATEGORY_AUDIT_FIELDS = ["id", "name"]
STOCK_MOVEMENT_AUDIT_FIELDS = [
    "id",
    "product_id",
    "location_id",
    "movement_type",
    "quantity_base",
    "unit_cost",
    "total_cost",
    "appointment_id",
    "created_by_id",
    "note",
    "is_void",
]


@backoffice_required
def backoffice_stock_dashboard_view(request):
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    products_qs = Product.objects.filter(is_active=True).annotate(
        stock=Coalesce(
            Sum("movements__quantity_base", filter=Q(movements__is_void=False)),
            Decimal("0.00"),
        )
    )

    low_stock = products_qs.filter(stock__lte=F("min_stock_alert")).order_by("stock", "name")
    low_stock_count = low_stock.count()

    consumption_week = (
        StockMovement.objects
        .filter(
            movement_type=StockMovement.TYPE_CONSUMPTION,
            is_void=False,
            created_at__date__gte=week_start,
            created_at__date__lte=today,
        )
        .aggregate(total=Coalesce(Sum("quantity_base"), Decimal("0.00")))
        .get("total")
        or Decimal("0.00")
    )
    consumption_month = (
        StockMovement.objects
        .filter(
            movement_type=StockMovement.TYPE_CONSUMPTION,
            is_void=False,
            created_at__date__gte=month_start,
            created_at__date__lte=today,
        )
        .aggregate(total=Coalesce(Sum("quantity_base"), Decimal("0.00")))
        .get("total")
        or Decimal("0.00")
    )

    consumption_week = abs(consumption_week)
    consumption_month = abs(consumption_month)

    top_consumed = list(
        StockMovement.objects
        .filter(
            movement_type=StockMovement.TYPE_CONSUMPTION,
            is_void=False,
            created_at__date__gte=month_start,
            created_at__date__lte=today,
        )
        .values("product__id", "product__name", "product__unit_base")
        .annotate(total=Coalesce(Sum("quantity_base"), Decimal("0.00")))
        .order_by("total")
    )[:5]
    for row in top_consumed:
        row["total_abs"] = abs(row.get("total") or Decimal("0.00"))

    return render(
        request,
        "backoffice/stock_dashboard.html",
        {
            "today": today,
            "low_stock": low_stock,
            "low_stock_count": low_stock_count,
            "consumption_week": consumption_week,
            "consumption_month": consumption_month,
            "top_consumed": top_consumed,
        },
    )


@backoffice_required
def backoffice_stock_products_list_view(request):
    q = (request.GET.get("q") or "").strip()
    category_id = (request.GET.get("category") or "").strip()
    per_page = request.GET.get("per_page") or "10"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 10

    qs = Product.objects.select_related("category").annotate(
        stock=Coalesce(
            Sum("movements__quantity_base", filter=Q(movements__is_void=False)),
            Decimal("0.00"),
        )
    )

    if q:
        qs = apply_terms_filter(qs, q, ["name__icontains", "sku__icontains"])
    if category_id:
        qs = qs.filter(category_id=category_id)

    qs = qs.order_by("name")

    paginator = None
    page_obj = None
    try:
        from django.core.paginator import Paginator

        paginator = Paginator(qs, per_page)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        rows = page_obj.object_list
    except Exception:
        rows = list(qs)

    categories = ProductCategory.objects.order_by("name")

    return render(
        request,
        "backoffice/stock_products_list.html",
        {
            "products": rows,
            "page_obj": page_obj,
            "paginator": paginator,
            "categories": categories,
            "q": q,
            "category_id": category_id,
            "per_page": per_page,
            "return_to": request.get_full_path(),
        },
    )


@backoffice_required
def backoffice_stock_categories_list_view(request):
    if not is_admin_role(request.user):
        return HttpResponseForbidden("Acesso reservado a administradores.")

    q = (request.GET.get("q") or "").strip()
    per_page = request.GET.get("per_page") or "10"
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in (5, 10, 15, 25, 50):
        per_page = 10

    qs = ProductCategory.objects.all()
    if q:
        qs = apply_terms_filter(qs, q, ["name__icontains"])
    qs = qs.order_by("name")

    paginator = None
    page_obj = None
    rows = list(qs)
    try:
        from django.core.paginator import Paginator

        paginator = Paginator(qs, per_page)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        rows = page_obj.object_list
    except Exception:
        pass

    return render(
        request,
        "backoffice/stock_categories_list.html",
        {
            "categories": rows,
            "page_obj": page_obj,
            "paginator": paginator,
            "q": q,
            "per_page": per_page,
            "return_to": request.get_full_path(),
        },
    )


@backoffice_required
def backoffice_stock_category_create_view(request):
    if not is_admin_role(request.user):
        return HttpResponseForbidden("Acesso reservado a administradores.")

    form = ProductCategoryForm(request.POST or None)
    return_to = request.GET.get("return_to") or ""
    if request.method == "POST" and form.is_valid():
        category = form.save()
        log_audit_event(
            category="product_category",
            action="create",
            request=request,
            instance=category,
            source="backoffice_stock",
            message="Categoria de produto criada.",
            after=snapshot_instance(category, PRODUCT_CATEGORY_AUDIT_FIELDS),
        )
        messages.success(request, "Categoria criada.")
        if return_to:
            return redirect(return_to)
        return redirect("backoffice_stock_categories")

    return render(
        request,
        "backoffice/stock_category_form.html",
        {
            "form": form,
            "title": "Nova categoria",
            "return_to": return_to,
        },
    )


@backoffice_required
def backoffice_stock_category_edit_view(request, category_id):
    if not is_admin_role(request.user):
        return HttpResponseForbidden("Acesso reservado a administradores.")

    category = get_object_or_404(ProductCategory, id=category_id)
    form = ProductCategoryForm(request.POST or None, instance=category)
    return_to = request.GET.get("return_to") or ""
    if request.method == "POST" and form.is_valid():
        before = snapshot_instance(category, PRODUCT_CATEGORY_AUDIT_FIELDS)
        category = form.save()
        log_audit_event(
            category="product_category",
            action="update",
            request=request,
            instance=category,
            source="backoffice_stock",
            message="Categoria de produto atualizada.",
            before=before,
            after=snapshot_instance(category, PRODUCT_CATEGORY_AUDIT_FIELDS),
        )
        messages.success(request, "Categoria atualizada.")
        if return_to:
            return redirect(return_to)
        return redirect("backoffice_stock_categories")

    return render(
        request,
        "backoffice/stock_category_form.html",
        {
            "form": form,
            "title": "Editar categoria",
            "return_to": return_to,
        },
    )


@backoffice_required
def backoffice_stock_product_create_view(request):
    form = ProductForm(request.POST or None)
    return_to = request.GET.get("return_to") or ""
    if request.method == "POST" and form.is_valid():
        product = form.save()
        log_audit_event(
            category="product",
            action="create",
            request=request,
            instance=product,
            source="backoffice_stock",
            message="Produto criado.",
            after=snapshot_instance(product, PRODUCT_AUDIT_FIELDS),
        )
        messages.success(request, "Produto criado.")
        if return_to:
            return redirect(return_to)
        return redirect("backoffice_stock_products")

    return render(
        request,
        "backoffice/stock_product_form.html",
        {
            "form": form,
            "title": "Novo produto",
            "return_to": return_to,
        },
    )


@backoffice_required
def backoffice_stock_product_edit_view(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    form = ProductForm(request.POST or None, instance=product)
    return_to = request.GET.get("return_to") or ""
    if request.method == "POST" and form.is_valid():
        before = snapshot_instance(product, PRODUCT_AUDIT_FIELDS)
        product = form.save()
        log_audit_event(
            category="product",
            action="update",
            request=request,
            instance=product,
            source="backoffice_stock",
            message="Produto atualizado.",
            before=before,
            after=snapshot_instance(product, PRODUCT_AUDIT_FIELDS),
        )
        messages.success(request, "Produto atualizado.")
        if return_to:
            return redirect(return_to)
        return redirect("backoffice_stock_products")

    return render(
        request,
        "backoffice/stock_product_form.html",
        {
            "form": form,
            "title": f"Editar produto",
            "return_to": return_to,
        },
    )


@backoffice_required
def backoffice_stock_entries_view(request):
    form = StockEntryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        product = form.cleaned_data["product"]
        quantity = form.cleaned_data["quantity"]
        unit_mode = form.cleaned_data["unit_mode"]
        unit_cost = form.cleaned_data.get("unit_cost")
        note = form.cleaned_data.get("note") or ""

        quantity_base = quantity
        if unit_mode == StockEntryForm.UNIT_PACK:
            if not product.unit_per_pack:
                form.add_error("quantity", "Este produto não tem unidade por embalagem definida.")
            else:
                quantity_base = quantity * product.unit_per_pack

        if not form.errors:
            location = get_default_location()
            movement = StockMovement.objects.create(
                product=product,
                location=location,
                movement_type=StockMovement.TYPE_PURCHASE,
                quantity_base=quantity_base,
                unit_cost=unit_cost,
                appointment=None,
                created_by=request.user,
                note=note,
            )
            log_audit_event(
                category="stock_movement",
                action="create_purchase",
                request=request,
                instance=movement,
                source="backoffice_stock_entries",
                message="Entrada de stock registada.",
                after=snapshot_instance(movement, STOCK_MOVEMENT_AUDIT_FIELDS),
                metadata={
                    "product_name": product.name,
                    "unit_mode": unit_mode,
                },
            )
            messages.success(request, "Entrada registada.")
            return redirect("backoffice_stock_entries")

    recent_entries = (
        StockMovement.objects
        .select_related("product", "created_by")
        .filter(movement_type=StockMovement.TYPE_PURCHASE, is_void=False)
        .order_by("-created_at")[:20]
    )

    return render(
        request,
        "backoffice/stock_entries.html",
        {
            "form": form,
            "recent_entries": recent_entries,
        },
    )


@backoffice_required
def backoffice_stock_movements_view(request):
    q = (request.GET.get("q") or "").strip()
    product_id = (request.GET.get("product") or "").strip()
    movement_type = (request.GET.get("type") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    qs = StockMovement.objects.select_related("product", "created_by", "appointment")

    if q:
        qs = apply_terms_filter(qs, q, ["product__name__icontains", "note__icontains"])
    if product_id:
        qs = qs.filter(product_id=product_id)
    if movement_type:
        qs = qs.filter(movement_type=movement_type)
    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d").date()
            qs = qs.filter(created_at__date__gte=dt)
        except Exception:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").date()
            qs = qs.filter(created_at__date__lte=dt)
        except Exception:
            pass

    qs = qs.order_by("-created_at")

    try:
        from django.core.paginator import Paginator

        paginator = Paginator(qs, 20)
        page_obj = paginator.get_page(request.GET.get("page") or 1)
        rows = page_obj.object_list
    except Exception:
        paginator = None
        page_obj = None
        rows = list(qs)

    products = Product.objects.order_by("name")

    return render(
        request,
        "backoffice/stock_movements.html",
        {
            "movements": rows,
            "page_obj": page_obj,
            "paginator": paginator,
            "products": products,
            "q": q,
            "product_id": product_id,
            "movement_type": movement_type,
            "date_from": date_from,
            "date_to": date_to,
        },
    )


@backoffice_required
def backoffice_stock_adjustments_view(request):
    form = StockAdjustmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        product = form.cleaned_data["product"]
        quantity = form.cleaned_data["quantity"]
        direction = form.cleaned_data["direction"]
        note = form.cleaned_data.get("note") or ""

        quantity_base = quantity
        if direction == StockAdjustmentForm.DIRECTION_DECREASE:
            quantity_base = quantity_base * Decimal("-1")

        location = get_default_location()
        movement = StockMovement.objects.create(
            product=product,
            location=location,
            movement_type=StockMovement.TYPE_ADJUSTMENT,
            quantity_base=quantity_base,
            created_by=request.user,
            note=note,
        )
        log_audit_event(
            category="stock_movement",
            action="create_adjustment",
            request=request,
            instance=movement,
            source="backoffice_stock_adjustments",
            message="Ajuste de stock registado.",
            after=snapshot_instance(movement, STOCK_MOVEMENT_AUDIT_FIELDS),
            metadata={
                "product_name": product.name,
                "direction": direction,
            },
        )
        messages.success(request, "Ajuste registado.")
        return redirect("backoffice_stock_adjustments")

    recent_adjustments = (
        StockMovement.objects
        .select_related("product", "created_by")
        .filter(movement_type=StockMovement.TYPE_ADJUSTMENT, is_void=False)
        .order_by("-created_at")[:20]
    )

    return render(
        request,
        "backoffice/stock_adjustments.html",
        {
            "form": form,
            "recent_adjustments": recent_adjustments,
        },
    )
