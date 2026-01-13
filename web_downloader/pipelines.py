# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


from urllib.parse import urlparse

from scrapy.pipelines.files import FilesPipeline


class PreservePathFilesPipeline(FilesPipeline):
    def file_path(self, request, response=None, info=None, *, item=None):
        parsed = urlparse(request.url)
        path = parsed.path.lstrip("/")
        if not path:
            return super().file_path(request, response=response, info=info, item=item)

        return path
