from django.contrib import admin

from .models import Author, Paper, Venue


@admin.register(Paper)
class PaperAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "year", "citation_count", "openalex_id", "arxiv_id", "doi")
    list_filter = ("year",)
    search_fields = ("title", "doi", "arxiv_id", "openalex_id")
    readonly_fields = ("updated_at",)


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "openalex_id", "affiliation")
    search_fields = ("name", "openalex_id")


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "venue_type")
    search_fields = ("name",)
