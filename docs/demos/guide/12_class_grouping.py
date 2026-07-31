import nebo as nb


@nb.fn()
class DataPipeline:
    def load(self):
        nb.log_text("status", "Loading data")
        return [1, 2, 3]

    def transform(self, data):
        nb.log_text("status", f"Transforming {len(data)} items")
        return [x * 2 for x in data]

    def save(self, data):
        nb.log_text("status", f"Saving {len(data)} items")


p = DataPipeline()
data = p.load()
data = p.transform(data)
p.save(data)
