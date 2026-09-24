from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.conf import settings
from django.conf.urls.static import static
urlpatterns=[path('admin/',admin.site.urls),path('login/',auth_views.LoginView.as_view(template_name='linker/creator/login.html'),name='login'),path('logout/',auth_views.LogoutView.as_view(),name='logout'),path('shoppinghub/login/',auth_views.LoginView.as_view(template_name='linker/creator/login.html'),name='shoppinghub_login'),path('shoppinghub/logout/',auth_views.LogoutView.as_view(),name='shoppinghub_logout'),path('',include('linker.urls'))]
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
