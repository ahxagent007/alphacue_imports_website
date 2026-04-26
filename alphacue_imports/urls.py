from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('', include('store.urls', namespace='store')),
    path('', include('affiliate.urls', namespace='affiliate')),
    path('favicon.ico', RedirectView.as_view(url='/static/store/favicon.ico', permanent=True)),
    path('ckeditor/', include('ckeditor_uploader.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)