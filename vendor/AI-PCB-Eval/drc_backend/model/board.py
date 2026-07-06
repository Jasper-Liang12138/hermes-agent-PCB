from dataclasses import dataclass, field
from typing import List, Tuple, Optional

Point = Tuple[float, float]


@dataclass
class Net:
    id: int
    name: str


@dataclass
class Pad:
    id: str
    net: str
    x: float
    y: float
    layer: str
    component: str
    layer_id: int = -1
    is_bga: bool = False
    bga_row: Optional[str] = None
    bga_col: Optional[int] = None
    """0315新增属性：size_x, size_y, shape"""
    size_x: float = 0.0 
    size_y: float = 0.0
    shape: str = ""

    @property
    def p(self) -> Point:
        return (self.x, self.y)


@dataclass
class Via:
    id: str
    net: str
    x: float
    y: float
    drill: float = 0.0
    size: float = 0.0
    type: str = "THROUGH"
    start_layer: str = ""
    end_layer: str = ""
    start_layer_id: int = -1
    end_layer_id: int = -1

    @property
    def p(self) -> Point:
        return (self.x, self.y)


@dataclass
class Segment:
    id: str
    net: str
    layer: str
    width: float
    start: Point
    end: Point
    layer_id: int = -1


@dataclass
class Board:
    nets: List[Net] = field(default_factory=list)
    pads: List[Pad] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    modules: List[dict] = field(default_factory=list)
    layers_table: dict = field(default_factory=dict)