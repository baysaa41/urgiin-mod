"""HTMX-д зориулсан жижиг middleware."""

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse


class HtmxRedirectToLoginMiddleware:
    """HTMX хүсэлт нэвтрэх хуудас руу шилжих үед БҮТЭН хуудас шилжүүлнэ.

    HTMX нь 302-ыг XHR-ээр чимээгүй дагадаг тул нэвтрэх хуудсын HTML нь
    зорилтот элементийн дотор шургаж, хуудас эвдэрдэг — жишээ нь хайлтын
    үр дүнгийн хайрцагт нэвтрэх форм орж ирнэ. `HX-Redirect` толгой нь
    HTMX-д бүтэн хуудсыг шилжүүлэхийг заадаг.

    Энэ нь зөвхөн аноним хэрэглэгчийн хайлтад биш, **нэвтрэлтийн хугацаа
    дуусах** үед (хэрэглэгч хуудсан дээр байхад сесс алдагдаж, дараа нь
    HTMX товч дарах) ч ажиллана.

    `MIDDLEWARE`-д `LoginRequiredMiddleware`-ийн ӨМНӨ байрлана — ингэснээр
    түүний буцаасан хариуг дараа нь засах боломжтой.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.login_path = reverse(settings.LOGIN_URL)

    def __call__(self, request):
        response = self.get_response(request)

        if (
            request.headers.get("HX-Request")
            and response.status_code in {301, 302}
            and response.headers.get("Location", "").startswith(self.login_path)
        ):
            redirect = HttpResponse(status=204)
            redirect["HX-Redirect"] = response["Location"]
            return redirect

        return response
