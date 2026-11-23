from django.contrib import admin

from .models import (
    Campaign,
    Coupon,
    CouponFileUpload,
    CustomerFileUpload,
    ErrorLog,
    InstantCampaign,
    ProductFileUpload,
    ProductsUnNormalizedDataStaging,
    Tenant,
    UsersUnNormalizedDataStaging,
)

# Register your models here.

admin.site.register(Campaign)
admin.site.register(InstantCampaign)
admin.site.register(Tenant)
admin.site.register(CustomerFileUpload)
admin.site.register(ProductFileUpload)
admin.site.register(UsersUnNormalizedDataStaging)
admin.site.register(CouponFileUpload)
admin.site.register(ProductsUnNormalizedDataStaging)
admin.site.register(Coupon)
admin.site.register(ErrorLog)
