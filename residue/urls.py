﻿from django.conf.urls import url

from residue import views


urlpatterns = [
    url(r'^targetselection', views.TargetSelection.as_view(), name='targetselection'),
    url(r'^residuetable$', views.ResidueTablesSelection.as_view(), name='residuetableselect'),
    url(r'^residuetable_gprot$', views.ResidueGprotSelection.as_view(), name='residuetableselect_gprot'),
    url(r'^residuetable_arrestin$', views.ResidueArrestinSelection.as_view(), name='residuetableselect_arrestin'),
    url(r'^residuetabledisplay', views.ResidueTablesDisplay.as_view(), name='residuetable'),
    url(r'^residuetableexcel', views.render_residue_table_excel, name='residuetableexcel'),
    url(r'^residuefunctionbrowser$', views.ResidueFunctionBrowser.as_view(), name='residue_function_browser'),
]
