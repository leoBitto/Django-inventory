from django.db import models
from django.utils.translation import gettext_lazy as _
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.dispatch import receiver
from django.db.models.signals import post_delete
import uuid
from django.utils import timezone
from decimal import Decimal

if 'crm' in settings.INSTALLED_APPS:
    from crm.models.base import Customer, Supplier
else:
    Customer = None
    Supplier = None


class ProductCategory(models.Model):
    name = models.CharField(_("tipo"), max_length=50)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subcategories', verbose_name=_("categoria padre"))

    class Meta:
        verbose_name = _("categoria")
        verbose_name_plural = _("categorie")

    def __str__(self):
        return self.name
    

class Product(models.Model):
    name = models.CharField(_("nome"), max_length=200)
    internal_code = models.CharField(_("codice interno"), max_length=50, unique=True, db_index=True, editable=False)
    category = models.ForeignKey('ProductCategory', on_delete=models.SET_NULL, null=True, blank=True, related_name='products', verbose_name=_("categoria"))
    stock_quantity = models.PositiveIntegerField(_("quantità in stock"), default=0)
    is_visible = models.BooleanField(_("visibile nel sito web"), default=False)
    description = models.TextField(_("descrizione"), blank=True, null=True)

    class Meta:
        verbose_name = _("prodotto")
        verbose_name_plural = _("prodotti")
        indexes = [
            models.Index(fields=['name', 'internal_code']),
        ]

    def __str__(self):
        return f"{self.name} ({self.internal_code})"

    def save(self, *args, **kwargs):
        if not self.internal_code:
            self.internal_code = self.generate_internal_code()
        super().save(*args, **kwargs)

    def generate_internal_code(self):
        return f"PROD-{uuid.uuid4().hex[:8].upper()}"
    
    def calculate_average_purchase_price(self):
        total_price = Decimal('0')
        total_quantity = 0
        for order in self.order_transactions.filter(status='paid'):
            total_price += order.unit_price * order.quantity
            total_quantity += order.quantity
        return total_price / total_quantity if total_quantity > 0 else Decimal('0')

    def calculate_average_sales_price(self):
        total_price = Decimal('0')
        total_quantity = 0
        for sale in self.sale_transactions.filter(status='paid'):
            total_price += sale.unit_price * sale.quantity
            total_quantity += sale.quantity
        return total_price / total_quantity if total_quantity > 0 else Decimal('0')

    @property
    def average_purchase_price(self):
        return self.calculate_average_purchase_price()

    @property
    def average_sales_price(self):
        return self.calculate_average_sales_price()

    @property
    def average_profit(self):
        return (self.average_sales_price - self.average_purchase_price) if self.average_sales_price and self.average_purchase_price else Decimal('0')


    def update_stock(self, quantity):
        if self.stock_quantity + quantity < 0:
            raise ValidationError(_('Quantità di stock insufficiente per l\'operazione.'))

        # aggiorna la quantità
        self.stock_quantity += quantity

        self.save()

class ProductImage(models.Model):
    image = models.ImageField(_("immagine"), upload_to='products/', blank=True, null=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images', verbose_name=_("prodotto"))

    class Meta:
        verbose_name = _("immagine del prodotto")
        verbose_name_plural = _("immagini del prodotto")

    def __str__(self):
        return f"Immagine per {self.product.name}"

# Segnale per eliminare i file delle immagini quando una ProductImage viene eliminata
@receiver(post_delete, sender=ProductImage)
def delete_image_file(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(False)


class Transaction(models.Model):
    STATUS_CHOICES = [
        ('pending', _('In Attesa')),
        ('sold', _('Venduto')),
        ('delivered', _('Consegnato')),
        ('paid', _('Pagato')),
        ('cancelled', _('Annullato')),
    ]
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='%(class)s_transactions', verbose_name=_("prodotto"))
    sale_date = models.DateField(_("data vendita"), blank=True, null=True)
    delivery_date = models.DateField(_("data consegna"), blank=True, null=True)
    payment_date = models.DateField(_("data pagamento"), blank=True, null=True)
    quantity = models.PositiveIntegerField(_("pezzi"))
    unit_price = models.DecimalField(_("prezzo unitario"), max_digits=10, decimal_places=2)
    status = models.CharField(_("stato"), max_length=20, choices=STATUS_CHOICES, default='pending')

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['sale_date'], name='sale_date_idx'),
        ]
        ordering = ['sale_date']

    def clean(self):
        if self.sale_date and self.delivery_date and self.sale_date > self.delivery_date:
            raise ValidationError(_('La data di vendita deve essere prima della data di consegna.'))
        if self.sale_date and self.payment_date and self.payment_date < self.sale_date:
            raise ValidationError(_('La data di pagamento non può essere anteriore alla data di vendita.'))
        if self.unit_price < Decimal('0'):
            raise ValidationError(_('Il prezzo unitario non può essere negativo.'))
        
        if self.status != 'cancelled':
            if self.payment_date:
                self.status = 'paid'
            elif self.delivery_date:
                self.status = 'delivered'
            elif self.sale_date:
                self.status = 'sold'
            else:
                self.status = 'pending'
        else:
            # Se lo stato è annullato, non cambiare in base alle date
            pass

    @property
    def total_price(self):
        return self.quantity * self.unit_price



class Sale(Transaction):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, null=True, blank=True, related_name='sales', verbose_name=_("cliente"))
    invoice = models.FileField(_("Fattura"), upload_to='invoices/sales/', blank=True)
    delivery_note = models.FileField(_("Bolla di accompagnamento"), upload_to='delivery_notes/sales/', blank=True)
    sale_invoice_number = models.CharField(
        _("Numero Fattura Vendita"),
        max_length=30,
        blank=True,
        null=True,
        help_text="Numero della fattura di vendita (es. 2024-001)"
    )

    class Meta:
        verbose_name = _("vendita")
        verbose_name_plural = _("vendite")

    def __str__(self):
        return _("Vendita a {customer} - {product}").format(customer=self.customer.name if self.customer else 'N/A', product=self.product.name)

    def save(self, *args, **kwargs):
        # Check if sale_date is in the past before updating stock
        if self.sale_date and self.sale_date < timezone.now().date():
            if self.status == 'sold':
                self.product.update_stock(-self.quantity)
            elif self.status == 'cancelled':
                # Revert stock change if sale was cancelled
                original = Sale.objects.filter(pk=self.pk).first()
                if original and original.status != 'cancelled':
                    self.product.update_stock(original.quantity)
        super().save(*args, **kwargs)


class Order(Transaction):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, null=True, blank=True, related_name='orders', verbose_name=_("fornitore"))
    supplier_product_code = models.CharField(_("Codice Interno del Fornitore"),max_length=30, blank=True, null=True)
    invoice = models.FileField(_("Fattura"), upload_to='invoices/orders/', blank=True)
    delivery_note = models.FileField(_("Bolla di accompagnamento"), upload_to='delivery_notes/orders/', blank=True)
    order_invoice_number = models.CharField(
        _("Numero Fattura Ordine"),
        max_length=30,
        blank=True,
        null=True,
        help_text="Numero della fattura di ordine (es. 2024-001)"
    )

    class Meta:
        verbose_name = _("ordine")
        verbose_name_plural = _("ordini")

    def __str__(self):
        return _("Ordine da {supplier} - {product}").format(supplier=self.supplier.name if self.supplier else 'N/A', product=self.product.name)

    def save(self, *args, **kwargs):
        original = Order.objects.filter(pk=self.pk).first()

        if self.status == 'delivered':
            if original is None or original.status != 'delivered':
                # Aggiorna lo stock e il prezzo medio di acquisto
                self.product.update_stock(self.quantity)

        elif self.status == 'cancelled' and original and original.status == 'delivered':
            self.product.update_stock(-self.quantity)

        super().save(*args, **kwargs)


@receiver(post_delete, sender=Sale)
def delete_files_on_sale_delete(sender, instance, **kwargs):
    if instance.invoice:
        instance.invoice.delete(False)
    if instance.delivery_note:
        instance.delivery_note.delete(False)

@receiver(post_delete, sender=Order)
def delete_files_on_order_delete(sender, instance, **kwargs):
    if instance.invoice:
        instance.invoice.delete(False)
    if instance.delivery_note:
        instance.delivery_note.delete(False)

        