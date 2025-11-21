"""Notebook utility functions for Jupyter environments and interactive development."""

import json
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from genericpath import exists
from typing import Any, Optional, Union
import pandas as pd

from .base_utils import BaseUtils


class NumberType(Enum):
    """Enumeration of valid number types for data objects."""
    NR = "NR"
    AA = "AA"
    LOG2 = "Log2"


class DataType(Enum):
    """Enumeration of valid data types for data objects."""
    TRANS = "TRANS"
    PROT = "PROT"
    MGR = "MGR"


@dataclass
class DataObject:
    """Data object with metadata for standardized naming and provenance tracking.

    Attributes:
        name: Human-readable name for the data object
        prefix: Required prefix for filename generation
        data: The actual data content
        source_file: Optional source file path (must be in data/ directory)
        number_type: Optional number type (NR, AA, Log2)
        data_type: Optional data type (TRANS, PROT, MGR)
        kb_metadata: Optional metadata from KBase objects
    """
    prefix: str
    data: Any
    name: Optional[str] = None
    source_file: Optional[str] = None
    number_type: Optional[NumberType] = None
    data_type: Optional[DataType] = None
    kb_metadata: list = field(default_factory=list)

    # Class attribute to identify DataObject JSON
    _dataobject_marker: str = field(default="DataObject_v1", init=False)

    def to_dict(self) -> dict:
        """Convert DataObject to a dictionary suitable for JSON serialization.

        Returns:
            Dictionary representation of the DataObject
        """
        return {
            "_dataobject_marker": self._dataobject_marker,
            "name": self.name,
            "prefix": self.prefix,
            "source_file": self.source_file,
            "number_type": self.number_type.value if self.number_type else None,
            "data_type": self.data_type.value if self.data_type else None,
            "kb_metadata": self.kb_metadata,
            "data": self.data,
        }

    def to_json(self, indent: int = 4) -> str:
        """Serialize DataObject to JSON string.

        Args:
            indent: Indentation level for pretty printing

        Returns:
            JSON string representation
        """
        return json.dumps(self.to_dict(), indent=indent, skipkeys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "DataObject":
        """Create DataObject from a dictionary.

        Args:
            data: Dictionary containing DataObject fields

        Returns:
            DataObject instance

        Raises:
            ValueError: If required fields are missing
        """
        if "prefix" not in data:
            raise ValueError("DataObject requires 'prefix' field")
        if "data" not in data:
            raise ValueError("DataObject requires 'data' field")

        # Convert string enum values back to enum types
        number_type = None
        if data.get("number_type"):
            number_type = NumberType(data["number_type"])

        data_type = None
        if data.get("data_type"):
            data_type = DataType(data["data_type"])

        return cls(
            name=data.get("name"),
            prefix=data["prefix"],
            source_file=data.get("source_file"),
            number_type=number_type,
            data_type=data_type,
            kb_metadata=data.get("kb_metadata", []),
            data=data["data"],
        )

    @classmethod
    def from_json(cls, json_str: str) -> "DataObject":
        """Deserialize DataObject from JSON string.

        Args:
            json_str: JSON string representation

        Returns:
            DataObject instance
        """
        return cls.from_dict(json.loads(json_str))

    @staticmethod
    def is_dataobject_dict(data: Any) -> bool:
        """Check if a dictionary represents a DataObject.

        Args:
            data: Data to check

        Returns:
            True if data is a dict with DataObject marker and required fields
        """
        if not isinstance(data, dict):
            return False
        return (
            data.get("_dataobject_marker") == "DataObject_v1"
            and "prefix" in data
            and "data" in data
        )

    def generate_filename(self) -> str:
        """Generate standardized filename from metadata.

        Returns:
            Filename string (without .json extension)
        """
        parts = [self.prefix]
        if self.number_type:
            parts.append(self.number_type.value)
        if self.data_type:
            parts.append(self.data_type.value)
        return "-".join(parts)


class NotebookUtils(BaseUtils):
    """Utility functions for working with Jupyter notebooks and interactive environments.

    Provides tools for notebook manipulation, output formatting, interactive displays,
    and integration with notebook-specific features like widgets and displays.
    """

    def __init__(self, notebook_folder: str, **kwargs: Any) -> None:
        """Initialize notebook utilities.

        Args:
            **kwargs: Additional keyword arguments passed to BaseUtil
        """
        super().__init__(**kwargs)
        self.notebook_folder = notebook_folder
        self.data_dir = os.path.join(notebook_folder, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.datacache_dir = os.path.join(notebook_folder, "datacache")
        os.makedirs(self.datacache_dir, exist_ok=True)
        self.output_dir = os.path.join(notebook_folder, "nboutput")
        os.makedirs(self.output_dir, exist_ok=True)

        self.in_notebook = self._detect_notebook_environment()
        if self.in_notebook:
            self.log_info("Notebook environment detected")
        else:
            self.log_debug("Not running in notebook environment")

    def _detect_notebook_environment(self) -> bool:
        """Detect if code is running in a Jupyter notebook environment.

        Returns:
            True if running in a notebook, False otherwise
        """
        try:
            # Check for IPython/Jupyter kernel
            from IPython import get_ipython

            ipython = get_ipython()

            if ipython is None:
                return False

            # Check if we're in a notebook (not just IPython shell)
            return hasattr(ipython, "kernel")
        except ImportError:
            return False

    def display_dataframe(
        self,
        df: Any,
        max_rows: Optional[int] = None,
        max_cols: Optional[int] = None,
        use_interactive: bool = True,
        page_size: int = 25,
        show_search: bool = True,
        show_info: bool = True,
        scrollable: bool = True,
        height: Optional[str] = None,
    ) -> None:
        """Display a DataFrame with enhanced interactive features including pagination and search.

        Args:
            df: DataFrame to display (pandas, polars, etc.)
            max_rows: Maximum number of rows to display (None for all)
            max_cols: Maximum number of columns to display (None for all)
            use_interactive: Use interactive table with pagination and search
            page_size: Number of rows per page when using interactive display
            show_search: Show search functionality
            show_info: Show table information (row count, etc.)
            scrollable: Make table scrollable for large content
            height: Fixed height for the table (e.g., "400px")
        """
        if not self.in_notebook:
            print(df)
            return

        # Convert to pandas if not already
        if not isinstance(df, pd.DataFrame):
            try:
                # Try to convert other dataframe types to pandas
                if hasattr(df, "to_pandas"):
                    df = df.to_pandas()
                elif hasattr(df, "toPandas"):
                    df = df.toPandas()
                else:
                    df = pd.DataFrame(df)
            except Exception:
                # Fallback to simple display
                try:
                    from IPython.display import display

                    display(df)
                except ImportError:
                    print(df)
                return

        # Apply row/column limits if specified
        if max_rows and len(df) > max_rows:
            df = df.head(max_rows)
            self.log_info(f"Displaying first {max_rows} rows of {len(df)} total")

        if max_cols and len(df.columns) > max_cols:
            df = df.iloc[:, :max_cols]
            self.log_info(f"Displaying first {max_cols} columns of {len(df.columns)} total")

        #if use_interactive and len(df) > page_size:
        self._display_interactive_dataframe(
            df, page_size, show_search, show_info, scrollable, height
        )
        #else:
        #    self._display_simple_dataframe(df)

    def _display_interactive_dataframe(
        self,
        df: Any,
        page_size: int,
        show_search: bool,
        show_info: bool,
        scrollable: bool,
        height: Optional[str],
    ) -> None:
        """Display DataFrame with interactive features using itables."""
        try:
            # Use itables for interactive display
            import itables
            from IPython.display import display

            # Configure itables options
            itables.options.lengthMenu = [10, 25, 50, 100, -1]
            itables.options.pageLength = page_size
            itables.options.searching = show_search
            itables.options.info = show_info
            itables.options.scrollCollapse = True
            itables.options.scrollX = scrollable
            
            # Configure table width and text wrapping
            itables.options.style = "width:100%; table-layout: fixed;"
            itables.options.columnDefs = [
                {
                    "targets": "_all",
                    "className": "text-wrap"
                }
            ]
            
            if height:
                itables.options.scrollY = height

            # Display with itables (remove conflicting nowrap class)
            display(itables.show(df, classes="display"))
            
            # Add custom CSS for better text wrapping
            from IPython.display import HTML
            display(HTML("""
            <style>
            .dataTable td {
                word-wrap: break-word;
                word-break: break-word;
                white-space: normal !important;
                max-width: 200px;
            }
            .dataTable th {
                word-wrap: break-word;
                word-break: break-word;
                white-space: normal !important;
            }
            .text-wrap {
                white-space: normal !important;
            }
            </style>
            """))
            return

        except ImportError:
            # Clear error message about missing itables
            self.log_error(
                "Interactive table features require 'itables'. "
                "Install with: pip install itables"
            )
            print("\n" + "="*60)
            print("INTERACTIVE FEATURES UNAVAILABLE")
            print("="*60)
            print("To enable interactive tables with pagination and search:")
            print("  pip install itables")
            print("\nAlternatively, use: pip install \"KBUtilLib[notebook]\"")
            print("="*60 + "\n")
            
            # Fall back to simple display
            self._display_simple_dataframe(df)

    def _display_simple_dataframe(self, df: Any) -> None:
        """Display DataFrame using basic IPython display."""
        try:
            from IPython.display import display
            display(df)
        except ImportError:
            print(df)

    def display_json(self, data: dict[str, Any], indent: int = 2) -> None:
        """Display JSON data with proper formatting in notebook.

        Args:
            data: Dictionary or JSON-serializable data
            indent: Indentation level for pretty printing
        """
        if not self.in_notebook:
            print(json.dumps(data, indent=indent))
            return

        try:
            from IPython.display import JSON, display

            display(JSON(data))
        except ImportError:
            print(json.dumps(data, indent=indent))

    def display_markdown(self, text: str) -> None:
        """Display markdown text in notebook.

        Args:
            text: Markdown formatted text
        """
        if not self.in_notebook:
            print(text)
            return

        try:
            from IPython.display import Markdown, display

            display(Markdown(text))
        except ImportError:
            print(text)

    def display_html(self, html: str) -> None:
        """Display HTML content in notebook.

        Args:
            html: HTML content string
        """
        if not self.in_notebook:
            print(html)
            return

        try:
            from IPython.display import HTML, display

            display(HTML(html))
        except ImportError:
            print(html)

    def create_progress_bar(self, total: int, description: str = "") -> Any:
        """Create a progress bar for notebook environments.

        Args:
            total: Total number of iterations
            description: Description text for the progress bar

        Returns:
            Progress bar object (tqdm if available, None otherwise)
        """
        if not self.in_notebook:
            return None

        try:
            from tqdm.notebook import tqdm

            return tqdm(total=total, desc=description)
        except ImportError:
            self.log_warning("tqdm not available for progress bars")
            return None

    def clear_output(self, wait: bool = False) -> None:
        """Clear notebook cell output.

        Args:
            wait: Whether to wait for new output before clearing
        """
        if not self.in_notebook:
            return

        try:
            from IPython.display import clear_output

            clear_output(wait=wait)
        except ImportError:
            pass

    def create_interactive_widget(self, widget_type: str, **kwargs: Any) -> Any:
        """Create an interactive widget for notebook use.

        Args:
            widget_type: Type of widget ('text', 'slider', 'dropdown', etc.)
            **kwargs: Widget-specific parameters

        Returns:
            Widget object if ipywidgets available, None otherwise
        """
        if not self.in_notebook:
            self.log_warning("Widgets only supported in notebook environments")
            return None

        try:
            import ipywidgets as widgets

            widget_map = {
                "text": widgets.Text,
                "textarea": widgets.Textarea,
                "slider": widgets.IntSlider,
                "float_slider": widgets.FloatSlider,
                "dropdown": widgets.Dropdown,
                "checkbox": widgets.Checkbox,
                "button": widgets.Button,
                "progress": widgets.IntProgress,
            }

            if widget_type in widget_map:
                return widget_map[widget_type](**kwargs)
            else:
                self.log_error(f"Unknown widget type: {widget_type}")
                return None

        except ImportError:
            self.log_warning("ipywidgets not available for interactive widgets")
            return None

    def save(
        self,
        data: Any,
        name: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> Optional[DataObject]:
        """Save data to a JSON file in the notebook data directory.

        If meta is provided, creates a DataObject with standardized naming.
        If meta is not provided, saves raw data with the given name (backwards compatible).

        Args:
            data: The data to save
            name: Filename (without extension) for simple saves (ignored if meta provided)
            meta: Optional metadata dictionary with fields:
                - prefix: str (required) - prefix for standardized filename
                - source_file: str (optional) - source file in data/ directory
                - number_type: str (optional) - one of 'NR', 'AA', 'Log2'
                - data_type: str (optional) - one of 'TRANS', 'PROT', 'MGR'
                - kb_metadata: list (optional) - metadata from KBase objects
                - name: str (optional) - human-readable name for the data

        Returns:
            DataObject if meta was provided, None otherwise

        Raises:
            ValueError: If neither name nor meta is provided, or if meta is invalid
        """
        if meta is not None:
            # Create DataObject with metadata
            data_obj = self._create_dataobject_from_meta(data, meta)
            filename = self.datacache_dir + "/" + data_obj.generate_filename() + ".json"
            dir_path = os.path.dirname(filename)
            os.makedirs(dir_path, exist_ok=True)
            with open(filename, "w") as f:
                json.dump(data_obj.to_dict(), f, indent=4, skipkeys=True)
            return data_obj
        else:
            # Backwards compatible simple save
            if name is None:
                raise ValueError("Either 'name' or 'meta' must be provided to save()")
            filename = self.datacache_dir + "/" + name + ".json"
            dir_path = os.path.dirname(filename)
            os.makedirs(dir_path, exist_ok=True)
            with open(filename, "w") as f:
                json.dump(data, f, indent=4, skipkeys=True)
            return None

    def _create_dataobject_from_meta(self, data: Any, meta: dict) -> DataObject:
        """Create a DataObject from data and metadata dictionary.

        Args:
            data: The actual data content
            meta: Metadata dictionary

        Returns:
            DataObject instance

        Raises:
            ValueError: If required fields are missing or invalid
        """
        if "prefix" not in meta:
            raise ValueError("meta dictionary requires 'prefix' field")

        # Validate source_file if provided
        source_file = meta.get("source_file")
        if source_file is not None:
            # Check that source_file is in the data/ directory
            expected_prefix = "data/"
            if not source_file.startswith(expected_prefix):
                raise ValueError(
                    f"source_file must be in data/ directory, got: {source_file}"
                )
            # Optionally verify file exists
            full_path = os.path.join(self.notebook_folder, source_file)
            if not exists(full_path):
                self.log_warning(f"source_file does not exist: {full_path}")

        # Parse number_type
        number_type = None
        if meta.get("number_type"):
            try:
                number_type = NumberType(meta["number_type"])
            except ValueError:
                raise ValueError(
                    f"Invalid number_type: {meta['number_type']}. "
                    f"Must be one of: NR, AA, Log2"
                )

        # Parse data_type
        data_type = None
        if meta.get("data_type"):
            try:
                data_type = DataType(meta["data_type"])
            except ValueError:
                raise ValueError(
                    f"Invalid data_type: {meta['data_type']}. "
                    f"Must be one of: TRANS, PROT, MGR"
                )

        return DataObject(
            prefix=meta["prefix"],
            data=data,
            name=meta.get("name"),
            source_file=source_file,
            number_type=number_type,
            data_type=data_type,
            kb_metadata=meta.get("kb_metadata", []),
        )

    def load(
        self,
        name_or_meta: Union[str, dict],
        default: Any = None,
        kb_type: Optional[str] = None,
    ) -> Union[Any, DataObject]:
        """Load data from a JSON file in the notebook data directory.

        Automatically detects if the loaded data is a DataObject and returns
        the appropriate type.

        Args:
            name_or_meta: Either a filename (string, without extension) or a
                metadata dictionary with fields used to construct the filename:
                - prefix: str (required)
                - number_type: str (optional) - one of 'NR', 'AA', 'Log2'
                - data_type: str (optional) - one of 'TRANS', 'PROT', 'MGR'
            default: Default value to return if file doesn't exist
            kb_type: Optional KBase type for object factory construction

        Returns:
            DataObject if the loaded data is a DataObject, otherwise raw data
            (or the kb_type constructed object if kb_type is specified)

        Raises:
            ValueError: If file doesn't exist and no default provided,
                or if meta dict is missing required fields
        """
        # Determine filename based on input type
        if isinstance(name_or_meta, dict):
            filename = self._filename_from_meta(name_or_meta)
        elif isinstance(name_or_meta, str):
            filename = self.datacache_dir + "/" + name_or_meta + ".json"
        else:
            raise ValueError(
                f"name_or_meta must be a string or dict, got: {type(name_or_meta)}"
            )

        if not exists(filename):
            if default is None:
                self.log_error(f"Requested data doesn't exist at {filename}")
                raise ValueError(f"Requested data doesn't exist at {filename}")
            return default

        with open(filename) as f:
            data = json.load(f)

        # Check if data is a DataObject
        if DataObject.is_dataobject_dict(data):
            return DataObject.from_dict(data)

        # Apply kb_type if specified (backwards compatible)
        if kb_type is not None:
            data = self.kb_object_factory._build_object(kb_type, data, None, None)

        return data

    def _filename_from_meta(self, meta: dict) -> str:
        """Generate full file path from metadata dictionary.

        Args:
            meta: Metadata dictionary with prefix, number_type, data_type

        Returns:
            Full file path

        Raises:
            ValueError: If prefix is missing
        """
        if "prefix" not in meta:
            raise ValueError("meta dictionary requires 'prefix' field")

        parts = [meta["prefix"]]

        if meta.get("number_type"):
            # Validate number_type
            try:
                nt = NumberType(meta["number_type"])
                parts.append(nt.value)
            except ValueError:
                raise ValueError(
                    f"Invalid number_type: {meta['number_type']}. "
                    f"Must be one of: NR, AA, Log2"
                )

        if meta.get("data_type"):
            # Validate data_type
            try:
                dt = DataType(meta["data_type"])
                parts.append(dt.value)
            except ValueError:
                raise ValueError(
                    f"Invalid data_type: {meta['data_type']}. "
                    f"Must be one of: TRANS, PROT, MGR"
                )

        filename = "-".join(parts)
        return self.datacache_dir + "/" + filename + ".json"

    def list(self):
        """List all JSON files in the notebook data directory."""
        if not exists(self.datacache_dir):
            return []
        files = os.listdir(self.datacache_dir)
        return [x.split(".")[0] for x in files if x.endswith(".json")]

    def exists(self, name: str) -> bool:
        """Check if a JSON file exists in the notebook data directory."""
        return exists(self.datacache_dir + "/" + name + ".json")
