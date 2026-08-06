import math

import nebo as nb

for step in range(50):
    nb.log_line("sine", math.sin(step / 5))
    nb.log_line("cosine", math.cos(step / 5))
