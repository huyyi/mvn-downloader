import scrapy
from scrapy.linkextractors import LinkExtractor
from scrapy.spiders import CrawlSpider, Rule

from web_downloader.items import WebDownloaderItem


class HttpdSpider(CrawlSpider):
    name = "httpd"

    rules = (
        # Follow only directory links; skip ./ and ../ entries
        Rule(
            LinkExtractor(allow=(r".*/$"), deny=(r"\./", r"\.\./")),
            callback="parse_directory",
            process_links=lambda links: [link for link in links if link.text not in {"./", "../"}],
            follow=True
        ),
    )

    def __init__(self, start_url=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not start_url:
            raise ValueError("start_url is required (e.g., -a start_url=https://example.com/files/)")

        normalized = start_url if start_url.endswith("/") else f"{start_url}/"
        self.start_urls = [normalized]

    def parse_start_url(self, response):
        return list(self.parse_directory(response))

    def parse_directory(self, response):
        links = response.css("a::attr(href)").getall()
        links = links[1:]
        for href in links:
            if href in {"./", "../"}:
                continue

            if href.endswith("/"):
                continue  # directories are followed by rules

            file_url = response.urljoin(href)
            item = WebDownloaderItem()
            item["file_urls"] = [file_url]
            item["source_url"] = response.url
            yield item
