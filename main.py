import argparse
import logging
import os
import re
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFile

# --- Application Metadata ---
APP_NAME = "shrink-epub"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Amit Srivastava <daoofcoding@proton.me>"
APP_LICENSE = "MIT"
APP_COPYRIGHT = f"{APP_NAME} {APP_VERSION} | Copyright (c) 2026 {APP_AUTHOR} | {APP_LICENSE} License"
APP_DESCRIPTION = "Reduce EPUB file size by converting internal images to WebP format."

# --- Configurable Defaults ---
WEBP_DEFAULT_QUALITY = 80
WEBP_DEFAULT_MAX_SIZE = 1200
WEBP_DEFAUTL_MIN_SIZE = 100
WEBP_DEFAULT_LEVEL = 6
SUPPORTED_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"})
LINK_CONTAINER_EXTS = frozenset({".xhtml", ".html", ".htm", ".opf", ".ncx", ".css"})

# --- Logger (Global Handle) ---
logger = logging.getLogger(APP_NAME)


# --- Regex ---
# Define the extensions that need conversion (excluding WebP itself)
_ext_to_replace = [ext for ext in SUPPORTED_IMAGE_EXTS if ext != ".webp"]
_ext_regex_str = "|".join(re.escape(ext) for ext in _ext_to_replace)
# Regex to find image extensions in text content
IMAGE_EXT_PATTERN = re.compile(f"(?i)({_ext_regex_str})")
# Regex to find image MIME types in OPF files
MIME_TYPE_PATTERN = re.compile(r"(?i)image/(jpeg|jpg|png|bmp|tiff)")


# --- Pillow ----
# Allow Pillow to process truncated files
ImageFile.LOAD_TRUNCATED_IMAGES = True


class AppError(Exception):
    """Custom exception for application-specific errors."""

    pass


@dataclass
class Args:
    """Configuration arguments for the EPUB shrinking tool.

    Args:
        ri_path: The path to the source EPUB file or directory.
        ro_path: The output directory where shrinked EPUB files will be placed.
        w_quality: The WebP compression quality (0-100).
        w_level: The WebP compression level (0-6).
        w_max_size: The maximum width/height for the WebP images in pixels.
        l_silent: If True, set logging level to ERROR (suppress INFO/DEBUG).
        l_verbose: If True, set logging level to DEBUG.
    """

    ri_path: Path
    ro_path: Path
    w_quality: int
    w_level: int
    w_max_size: int
    l_silent: bool
    l_verbose: bool


@dataclass
class FileTask:
    """Represents a single file task for shrinking.

    Args:
        i_file: The path to the input file (e.g., an EPUB).
        o_file: The corresponding path for the output file.
    """

    i_file: Path
    o_file: Path


def setup_logging(silent: bool, verbose: bool) -> None:
    """Sets up the global logging configuration.

    Args:
        silent: If True, sets the log level to ERROR.
        verbose: If True, sets the log level to DEBUG.
    """
    if silent:
        log_level = logging.ERROR
    elif verbose:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level,
        format="%(levelname)-8s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def parse_args() -> Args:
    """Parses command-line arguments.

    Returns:
        An Args object populated with parsed command-line settings.

    Returns:
        Args:
            None

    Raises:
        argparse.ArgumentError: If required arguments are missing or values are
            out of bounds.
    """
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=APP_DESCRIPTION,
        epilog=APP_COPYRIGHT,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Metadata flags
    parser.add_argument("-v", "--version", action="version", version=APP_COPYRIGHT)

    # Core required paths
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        required=True,
        metavar="PATH",
        dest="ri_path",
        help="Source EPUB file or directory.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        dest="ro_path",
        help="Output directory for shrinked epub files.",
    )

    # Optimization settings
    parser.add_argument(
        "-q",
        "--quality",
        type=int,
        default=WEBP_DEFAULT_QUALITY,
        metavar="0-100",
        dest="w_quality",
        help="WebP compression quality.",
    )
    parser.add_argument(
        "-l",
        "--level",
        type=int,
        default=WEBP_DEFAULT_LEVEL,
        metavar="0-6",
        dest="w_level",
        help="WebP compression level.",
    )
    parser.add_argument(
        "-m",
        "--max-size",
        type=int,
        default=WEBP_DEFAULT_MAX_SIZE,
        metavar="PX",
        dest="w_max_size",
        help="Webp ax width/height.",
    )

    # Logging controls
    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "-s",
        "--silent",
        action="store_true",
        dest="l_silent",
        help="Suppress output except for errors.",
    )
    log_group.add_argument(
        "-V",
        "--verbose",
        action="store_true",
        dest="l_verbose",
        help="Show debug logs.",
    )

    args = parser.parse_args()
    # Resolve paths to absolute paths for consistency
    args.ri_path = args.ri_path.resolve()
    args.ro_path = args.ro_path.resolve()

    return Args(**vars(args))


def validate_args(args: Args) -> None:
    """Validates the parsed arguments to ensure they are within acceptable bounds.

    Args:
        args: The Args object containing configuration settings.

    Raises:
        AppError: If the input path is invalid, input and output paths conflict,
            or optimization parameters are out of range.
    """
    if not args.ri_path.exists():
        raise AppError(f"Input Path does not exist: {args.ri_path}")
    if args.ri_path == args.ro_path:
        raise AppError("Input and output cannot be the same directory.")

    # Validate WebP quality range
    if not (0 <= args.w_quality <= 100):
        raise AppError(f"Quality {args.w_quality} is out of bounds (0-100).")

    # Validate WebP compression level range
    if not (0 <= args.w_level <= 6):
        raise AppError(f"Compression level {args.w_level} is out of bounds (0-6).")

    # Validate maximum image size
    if args.w_max_size < WEBP_DEFAUTL_MIN_SIZE:
        raise AppError(
            f"Max webp size is too small. Minimum accepted value is {WEBP_DEFAUTL_MIN_SIZE} (px)."
        )


def scan_files(i_path: Path, o_path: Path) -> list[FileTask]:
    """Scans the input path for EPUB files and determines corresponding output paths.

    Args:
        i_path: The root path (EPUB file or directory) to scan.
        o_path: The root output directory.

    Returns:
        A list of FileTask objects, one for each EPUB file found.

    Returns:
        list[FileTask]: List of discovered file tasks.

    Raises:
        AppError: If the provided input path is not an EPUB file or directory.
    """
    files: list[FileTask] = []
    logger.debug("Initializing scan at raw path: %s", i_path)

    if i_path.is_file():
        if i_path.suffix.lower() == ".epub":
            logger.info("Path provided is an epub file: %s", i_path.name)
            files.append(FileTask(i_path, o_path.joinpath(i_path.name)))
        else:
            raise AppError(f"File provided is not an EPUB: {i_path}")
        return files

    logger.info("Scanning for EPUBs in: %s", i_path)
    # Recursively find all EPUB files
    for f in i_path.rglob("*.epub"):
        # Calculate the relative path to maintain the directory structure in the output
        o_file = o_path.joinpath(f.relative_to(i_path))
        # Ensure the output subdirectory exists
        o_file.parent.mkdir(parents=True, exist_ok=True)
        files.append(FileTask(f, o_file))

    return files


class EpubShrinker:
    """Handles the core logic of shrinking an EPUB file.

    The process involves unpacking the EPUB, converting internal images to WebP,
    updating image references, and repacking the file.
    """

    def __init__(self, args: Args) -> None:
        """Initializes the shrinker with configuration arguments.

        Args:
            args: The Args object containing WebP and logging settings.
        """
        self.args = args

    def run(self, f_tasks: list[FileTask]) -> None:
        """Executes the shrinking process for a list of file tasks.

        Uses a ThreadPoolExecutor to process files concurrently.

        Args:
            f_tasks: A list of FileTask objects to be processed.
        """
        s_count = 0
        e_count = 0
        failed: list[Path] = []
        # Use a thread pool sized by CPU count or default to 4 for concurrency
        with ThreadPoolExecutor(max_workers=(os.cpu_count() or 4)) as executor:
            futures = {
                executor.submit(self._shrink, f_task): f_task for f_task in f_tasks
            }
            # Wait for all threads to complete
            for future in as_completed(futures):
                f_task = futures[future]
                try:
                    future.result()
                    s_count += 1
                    logger.info("Successfully shrunk: %s", f_task.i_file.name)
                except Exception as e:
                    e_count += 1
                    logger.error("Failed to shrink %s: %s", f_task.i_file.name, e)
                    failed.append(f_task.i_file)

        logger.info(
            "Total: %s, Sucess: %s and Failed: %s.", len(f_tasks), s_count, e_count
        )

        if e_count:
            for f in failed:
                logger.error("Failed: %s", f)

    def _shrink(self, f_task: FileTask) -> None:
        """Performs the full shrink operation for a single file.

        Unpacks the EPUB, identifies images and links, converts images,
        updates links, and repacks the modified content.

        Args:
            f_task: The FileTask object defining the input and output files.
        """
        logger.debug("Processing: %s", f_task.i_file.name)

        # Use a temporary directory to unpack the EPUB safely
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t_dir:
            t_path = Path(t_dir)
            self._unpack(f_task.i_file, t_path)

            imgs: list[Path] = []
            refs: list[Path] = []

            # Scan the unpacked content to categorize files
            for f_path in t_path.rglob("*"):
                if f_path.is_file():
                    ext = f_path.suffix.lower()
                    if ext in SUPPORTED_IMAGE_EXTS:
                        imgs.append(f_path)
                    elif ext in LINK_CONTAINER_EXTS:
                        refs.append(f_path)

            # Process images and references
            self._update_images(imgs)
            self._update_refs(refs)
            # Repack the modified files into the output EPUB
            self._repack(t_path, f_task.o_file)

    def _unpack(self, s_path: Path, d_path: Path) -> None:
        """Unzips the EPUB file into a temporary directory.

        Also checks for potential path traversal vulnerabilities during extraction.

        Args:
            s_path: The path to the source EPUB file.
            d_path: The path to the destination temporary directory.

        Raises:
            AppError: If a path traversal attempt is detected.
        """
        with zipfile.ZipFile(s_path, "r") as z_ref:
            for m in z_ref.namelist():
                m_path = d_path.joinpath(m).resolve()
                # Security check: ensure the extracted path starts within the temp directory
                if not str(m_path).startswith(str(d_path.resolve())):
                    raise AppError(
                        f"Security Warning: Attempted path traversal in {s_path.name}"
                    )
                z_ref.extract(m, d_path)

    def _update_images(self, imgs: list[Path]) -> None:
        """Converts images to WebP format and resizes them.

        Images are opened, converted to RGBA if needed, resized using thumbnail
        to fit within `w_max_size x w_max_size`, and saved as WebP.

        Args:
            imgs: A list of paths to the image files to be processed.
        """
        for img in imgs:
            with Image.open(img) as i_file:
                # Convert to RGBA if the image mode is Palette (P) or lacks transparency
                if i_file.mode == "P" or i_file.mode not in ("RGB", "RGBA"):
                    i_file = i_file.convert("RGBA")

                # Resize image to fit within the max dimension using high-quality resampling
                i_file.thumbnail(
                    (self.args.w_max_size, self.args.w_max_size),
                    Image.Resampling.LANCZOS,
                )

                n_path = img.with_suffix(".webp")
                # Save the image as WebP with configured quality and compression level
                i_file.save(
                    n_path,
                    "webp",
                    quality=self.args.w_quality,
                    method=self.args.w_level,
                )

            # If the new WebP file is different from the original, delete the original
            if not img.samefile(n_path):
                img.unlink()

    def _update_refs(self, refs: list[Path]) -> None:
        """Updates image and media links within the EPUB content.

        Replaces image extensions in text content with `.webp` and updates
        OPF files to use the `image/webp` MIME type.

        Args:
            refs: A list of paths to the reference files (XHTML, OPF, CSS, etc.).
        """
        for r_path in refs:
            content = r_path.read_text(encoding="utf-8")
            # Replace common image extensions in HTML/XHTML content
            content = IMAGE_EXT_PATTERN.sub(".webp", content)

            # Update MIME type in OPF files to correctly reference WebP images
            if r_path.suffix.lower() == ".opf":
                content = MIME_TYPE_PATTERN.sub("image/webp", content)

            r_path.write_text(content, encoding="utf-8")

    def _repack(self, s_path: Path, d_path) -> None:
        """Repacks the temporary directory content into the final output EPUB.

        Handles the placement of the crucial 'mimetype' file and updates
        file paths (arcnames) for all contained assets.

        Args:
            s_path: The path to the source unpacked directory.
            d_path: The path to the final output EPUB file.
        """
        with zipfile.ZipFile(d_path, "w", zipfile.ZIP_DEFLATED) as epub:
            mimetype_path = s_path.joinpath("mimetype")
            # Mimetype must be stored without compression to be valid
            if mimetype_path.exists():
                epub.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)

            for f_path in s_path.rglob("*"):
                if f_path.is_file() and f_path.name != "mimetype":
                    # Calculate the archive name (arcname) which determines the file path inside the ZIP
                    arcname = f_path.relative_to(s_path)
                    # Ensure path separators are correct for ZIP files (forward slashes)
                    arcname_str = str(arcname).replace(os.sep, "/")
                    epub.write(f_path, arcname_str)


def main() -> None:
    """Main entry point of the EPUB shrinking tool.

    Parses arguments, sets up logging, validates inputs, scans for EPUBs,
    and executes the shrinking process.
    """
    try:
        args = parse_args()
        setup_logging(args.l_silent, args.l_verbose)
        validate_args(args)

        logger.info("Creating output directory: %s", args.ro_path)
        args.ro_path.mkdir(parents=True, exist_ok=True)

        files = scan_files(args.ri_path, args.ro_path)
        count = len(files)
        if not count:
            logger.warning("No EPUB files discovered in: %s", args.ri_path)
            logger.info("No work to perform. Exiting.")
            return

        logger.info("Found %d EPUB file(s).", count)

        # Debug output to show the full list of tasks
        for i in range(count):
            logger.debug("Input File:  %s", files[i].i_file)
            logger.debug("Output File: %s", files[i].o_file)

        shrinker = EpubShrinker(args)
        shrinker.run(files)

    except AppError as e:
        # Catch application-specific errors (validation, path issues)
        logger.error(e)
        sys.exit(1)
    except Exception as e:
        # Catch unexpected system errors
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
