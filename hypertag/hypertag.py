import os
from typing import List
from shutil import rmtree, move
import sqlite3
import json
from multiprocessing import Pool
from pathlib import Path
import fire  # type: ignore
from tqdm import tqdm  # type: ignore
import rpyc  # type: ignore
from .persistor import Persistor
from .graph import graph
from .utils import remove_symlink


class HyperTag:
    """ HyperTag CLI """

    def __init__(self):
        self.db = Persistor()
        self.root_dir = Path(self.db.get_hypertagfs_dir())
        os.makedirs(self.root_dir, exist_ok=True)

    def search_image(self, *text_queries: str, top_k=10, path=False, score=False, _return=False):
        """ Execute a semantic search that returns best matching images """
        text_query = " ".join(text_queries)
        try:
            rpc = rpyc.connect("localhost", 18861)
            results = rpc.root.search_image(text_query, path, top_k, score)
            for result in results:
                print(result)
            if len(result) == 0:
                print("No relevant files indexed...")
        except ConnectionRefusedError:
            from .vectorizer import CLIPVectorizer

            vectorizer = CLIPVectorizer()
            results = vectorizer.search_image(text_query, path, top_k, score)
        if _return:
            return results

    def index(self, text=None, image=None, rebuild=False, cache=False, cores: int = 0):
        """ Vectorize image & text files (needed for semantic search) """
        if (image and text is None) or (image and text) or (not image and not text):
            self.index_images()
        if (text and image is None) or (image and text) or (not image and not text):
            self.index_texts(rebuild, cache, cores)

    def index_images(self):
        """ Vectorize image files (needed for semantic search) """
        from .vectorizer import CLIPVectorizer, get_image_files

        file_paths = self.db.get_unindexed_file_paths()
        compatible_files = get_image_files(file_paths, verbose=True)
        print("Vectorizing", len(compatible_files), "images...")
        remote = True
        if remote:
            try:
                rpc = rpyc.connect("localhost", 18861)
                rpc._config["sync_request_timeout"] = None  # Disable timeout
                print("Connected to DaemonService successfully")
            except ConnectionRefusedError:
                print("DaemonService connection failed, falling back to local execution...")
                remote = False

        if not remote:
            img_vectorizer = CLIPVectorizer()

        for file_path in tqdm(compatible_files):
            if remote:
                img_vector = json.loads(rpc.root.encode_image(file_path))[0]
            else:
                img_vector = img_vectorizer.encode_image(file_path)[0].tolist()

            self.db.add_file_embedding_vector(file_path, json.dumps(img_vector))
            self.db.conn.commit()

    def index_texts(self, rebuild=False, cache=False, cores: int = 0):
        """ Vectorize text files (needed for semantic search) """
        # TODO: index images
        # TODO: auto index on file addition (import)
        from .vectorizer import TextVectorizer, extract_clean_text, get_text_documents

        print("Vectorizing text documents...")
        remote = True
        if remote:
            try:
                rpc = rpyc.connect("localhost", 18861)
                rpc._config["sync_request_timeout"] = None  # Disable timeout
                print("Connected to DaemonService successfully")
            except ConnectionRefusedError:
                print("DaemonService connection failed, falling back to local execution...")
                remote = False
        if cache:
            print("Caching cleaned texts (database will grow big)")
        if rebuild:
            print("Rebuilding index")
            file_paths = self.db.get_indexed_file_paths()
        else:
            file_paths = self.db.get_unindexed_file_paths()
        i = 0
        compatible_files = get_text_documents(file_paths, verbose=True)
        min_words = 5
        min_word_length = 4
        args = []
        for file_path, file_type in compatible_files:
            args.append((file_path, file_type, cache, min_words, min_word_length))
        inference_tuples = []

        # Preprocess using multi-processing (default uses all available cores)
        if cores <= 0:
            n_cores = os.cpu_count()
        else:
            n_cores = cores
        pool = Pool(processes=n_cores)
        print(f"Preprocessing texts using {n_cores} cores...")
        with tqdm(total=len(compatible_files)) as t:
            for file_path, sentences in pool.imap_unordered(extract_clean_text, args):
                t.update(1)
                if sentences:
                    inference_tuples.append((file_path, sentences))
        print(f"Cleaned {len(inference_tuples)} text doc/s successfully")
        print("Starting inference...")
        # Compute embeddings
        if not remote:
            vectorizer = TextVectorizer()
        for file_path, sentences in tqdm(inference_tuples):
            if remote:
                document_vector = json.loads(rpc.root.compute_text_embedding(json.dumps(sentences)))
            else:
                document_vector = vectorizer.compute_text_embedding(sentences)
            if (
                document_vector is not None
                and type(document_vector) is list
                and len(document_vector) > 0
            ):
                self.db.add_file_embedding_vector(file_path, json.dumps(document_vector))
                self.db.conn.commit()
                i += 1
            else:
                print(type(document_vector))
                self.db.add_file_embedding_vector(file_path, json.dumps([]))
                self.db.conn.commit()
                print("Failed to parse file - skipping:", file_path)
        print(f"Vectorized {str(i)} file/s successfully")

    def search(self, *text_queries: str, path=False, top_k=10, score=False, _return=False):
        """ Execute a semantic search that returns best matching text documents """
        text_query = " . ".join(text_queries)
        try:
            rpc = rpyc.connect("localhost", 18861)
            results = rpc.root.search(text_query, path, top_k, score)
            for result in results:
                print(result)
            if len(result) == 0:
                print("No relevant files indexed...")
        except ConnectionRefusedError:
            from .vectorizer import TextVectorizer

            vectorizer = TextVectorizer()
            results = vectorizer.search(text_query, path, top_k, score)
        if _return:
            return results

    def add_auto_import_dir(self, path: str):
        """ Add path for auto import directory (watched by daemon) """
        self.db.add_auto_import_directory(path)

    def set_hypertagfs_dir(self, path: str):
        """ Set path for HyperTagFS directory """
        self.db.set_hypertagfs_dir(path)

    def mount(self, root_dir=None, parent_tag_id=None):
        """ Generate HyperTagFS: tag representation using symlinks """
        if root_dir is None:
            root_dir = self.root_dir
        root_path = Path(root_dir)

        if parent_tag_id is None:
            print("Updating HyperTagFS...")
            graph()
            os.makedirs(root_path / "Search Texts", exist_ok=True)
            os.makedirs(root_path / "Search Images", exist_ok=True)
            tag_ids_names = self.db.get_root_tag_ids_names()
        else:
            tag_ids_names = self.db.get_tag_id_children_ids_names(parent_tag_id)

        leaf_tag_ids = {tag_id[0] for tag_id in self.db.get_leaf_tag_ids()}

        for tag_id, name in tag_ids_names:
            file_paths_names = self.db.get_file_paths_names_by_tag_id(tag_id)
            if len(file_paths_names) > 0:
                underscore_root_tag_path = root_path / ("_" + name)
                root_tag_path = root_path / name
                if not root_tag_path.exists():
                    os.makedirs(underscore_root_tag_path, exist_ok=True)
                    move(underscore_root_tag_path, root_tag_path)  # Needed for daemon
                symlink_path = root_tag_path
                if tag_id not in leaf_tag_ids:
                    symlink_path = root_tag_path / "_files"
                    os.makedirs(symlink_path, exist_ok=True)
                for file_path, file_name in file_paths_names:
                    try:
                        os.symlink(Path(file_path), symlink_path / file_name)
                    except FileExistsError:
                        pass
                self.mount(root_tag_path, tag_id)

    def auto_add_tags_from_path(
        self, file_path: str, import_path_dirs: List[str], verbose=False, keep_all=False
    ):
        file_path_tags = [p for p in str(file_path).split("/") if p not in import_path_dirs]
        if not keep_all:
            file_path_tags = file_path_tags[:-1]
        if verbose:
            print("Inferred tags:", file_path_tags)
        self.tag(
            file_path.name,
            "with",
            *file_path_tags,
            remount=False,
            add=False,
            commit=False,
        )
        for previous, current in zip(file_path_tags, file_path_tags[1:]):
            self.metatag(current, "with", previous, remount=False, commit=False)
        # print(file_path_tags, file_path.name)

    def import_tags(self, import_path: str, only_tags=False, verbose=False):
        """Import files with tags inferred from existing directory hierarchy
        (ignores hidden directories)"""
        file_paths = [p for p in list(Path(import_path).rglob("*")) if p.is_file()]
        # Remove files in hidden directories or in ignore list
        ignore_list = set(self.db.get_ignore_list())
        visible_file_paths = []
        for p in file_paths:
            is_hidden = False
            for pp in str(p).split("/"):
                if pp.startswith(".") or pp in ignore_list:
                    is_hidden = True
            if not is_hidden:
                visible_file_paths.append(p)
        print("Adding files...")
        if only_tags:
            added_file_paths = visible_file_paths
        else:
            added_file_paths = self.add(*visible_file_paths)
        import_path_dirs = set(str(import_path).split("/"))
        print("Adding tags...")
        for file_path in tqdm(added_file_paths):
            self.auto_add_tags_from_path(file_path, import_path_dirs, verbose)
        self.db.conn.commit()
        self.mount(self.root_dir)

    def remove(self, *file_names):
        """ Remove files """
        for file_name in tqdm(file_names):
            self.db.remove_file(file_name)
            remove_symlink(self.root_dir, file_name)
        self.mount(self.root_dir)

    def add(self, *file_paths):
        """ Add file/s """
        added = []
        for file_path in tqdm(file_paths):
            try:
                if Path(file_path).is_file():
                    self.db.add_file(os.path.abspath(file_path))
                    added.append(file_path)
            except sqlite3.IntegrityError:
                pass
        self.db.conn.commit()
        print("Added", len(added), "new file/s")
        return added

    def tags(self, *file_names):
        """ Display all tags of file/s """
        tags = set()
        for file_name in file_names:
            tags.update(set(self.db.get_tags_by_file_name(file_name)))
        for tag in tags:
            print(tag)

    def show(self, mode="tags", path=False):
        """ Display all tags (default), indexed files (mode=index) or files """
        if mode == "files":
            names = self.db.get_files(path)
        elif mode == "index":
            names = self.db.get_indexed_file_paths(path)
        elif mode == "tags":
            names = self.db.get_tags()
        for name in names:
            print(name)

    def query(self, *query, path=False, fuzzy=True, verbose=False):
        """Query files using set operands.
        Supported operands:
        and (intersection (default))
        or (union)
        minus (difference)
        """
        # TODO: Parse AST to support queries with brackets
        operands = {"and", "or", "minus"}
        results = set(self.db.get_files_by_tag(query[0], path, fuzzy, verbose))
        current_operand = None
        for query_symbol in query[1:]:
            if query_symbol not in operands:
                file_names = set(self.db.get_files_by_tag(query_symbol, path, fuzzy, verbose))
                if current_operand == "or":
                    results = results.union(file_names)
                elif current_operand == "minus":
                    results = results - file_names
                else:
                    # and (intersection) is default operand
                    results = results.intersection(file_names)
            else:
                current_operand = query_symbol
        return results

    def tag(self, *args, remount=True, add=True, commit=True):
        """ Tag file/s with tag/s """
        # Parse arguments
        file_names = []
        tags = []
        is_file_name = True
        for arg in args:
            if arg == "with":
                is_file_name = False
                continue
            if is_file_name:
                file_names.append(arg)
            else:
                tags.append(arg)
        if add:
            self.add(*file_names)
        # Add tags
        for file_name in file_names:
            file_name = file_name.split("/")[-1]
            for tag in tags:
                self.db.add_tag_to_file(tag, file_name)
            # print("Tagged", file_name, "with", tags)
        if commit:
            self.db.conn.commit()
        # Remount (everything is mounted)
        if remount:
            self.mount(self.root_dir)

    def untag(self, *args, remount=True, commit=True):
        """ Untag (remove tag/s of) file/s with tag/s """
        # Parse arguments
        file_names = []
        tags = []
        is_file_name = True
        for arg in args:
            if arg == "with":
                is_file_name = False
                continue
            if is_file_name:
                file_names.append(arg)
            else:
                tags.append(arg)
        # Remove tags
        for file_name in file_names:
            file_name = file_name.split("/")[-1]
            for tag in tags:
                self.db.remove_tag_from_file(tag, file_name)
            # print("Tagged", file_name, "with", tags)
        if commit:
            self.db.conn.commit()
        # TODO: Remove symlink (get all paths from metatags)
        # Remount (everything is mounted)
        if remount:
            self.mount(self.root_dir)

    def metatags(self, *tag_names):
        """ Display all metatags of tag/s """
        tags = set()
        for tag_name in tag_names:
            tags.update(set(self.db.get_meta_tags_by_tag_name(tag_name)))
        for tag in tags:
            print(tag)

    def metatag(self, *args, remount=True, commit=True):
        """ Tag tag/s with tag/s """
        # Parse arguments
        parent_tags = []
        tags = []
        is_parent_tag = False
        for arg in args:
            if arg == "with":
                is_parent_tag = True
                continue
            if is_parent_tag:
                parent_tags.append(arg)
            else:
                tags.append(arg)

        # Add tags
        for tag in tags:
            for parent_tag in parent_tags:
                self.db.add_parent_tag_to_tag(parent_tag, tag)
            # print("MetaTagged", tag, "with", parent_tags)
        if commit:
            self.db.conn.commit()

        for tag in tags:
            for parent_tag in parent_tags:
                # Remove parent_tag dir in all levels
                self.rmdir(self.root_dir, parent_tag)
                # Remove tag dir in root level
                try:
                    rmtree(self.root_dir / tag)
                except Exception:  # nosec
                    pass  # Ignore if non existing
        # Remount (everything is mounted)
        if remount:
            self.mount(self.root_dir)

    def rmdir(self, directory, del_dir_name):
        # Delete dirs named del_dir_name in directory recursively
        for item in directory.iterdir():
            if item.name == del_dir_name and item.is_dir():
                rmtree(item)
            if item.is_dir():
                self.rmdir(item, del_dir_name)

    def merge(self, tag_a, _into, tag_b):
        """ Merges all associations (files & tags) of tag_a into tag_b """
        print("Merging tag", tag_a, "into", tag_b)
        self.db.merge_tags(tag_a, tag_b)
        self.rmdir(self.root_dir, tag_a)
        self.mount(self.root_dir)


def daemon():
    """ Start daemon process watching HyperTagFS """
    print("Starting up daemon...")
    from .daemon import start
    from multiprocessing import Process, set_start_method

    set_start_method("spawn")
    p = Process(target=start)
    p.start()
    p.join()


def help():
    """ Get some help on how to use HyperTag """
    print(
        """
    README: https://github.com/SeanPedersen/HyperTag/blob/master/README.md
    Found a bug or simply feeling lost?
    Do not despair, you're not alone! Let us know at https://github.com/SeanPedersen/HyperTag/issues
    Chat and read about HyperTag using Matrix at https://bit.ly/2Xtz2Ff
    Print available CLI functions: hypertag
    Print available CLI flags for all commands: hypertag command --help
    """
    )


def main():
    ht = HyperTag()
    fire_cli = {
        "help": help,
        "add": ht.add,
        "remove": ht.remove,
        "import": ht.import_tags,
        "tag": ht.tag,
        "t": ht.tag,
        "untag": ht.untag,
        "metatag": ht.metatag,
        "tt": ht.metatag,
        "merge": ht.merge,
        "show": ht.show,
        "tags": ht.tags,
        "metatags": ht.metatags,
        "query": ht.query,
        "q": ht.query,
        "set_hypertagfs_dir": ht.set_hypertagfs_dir,
        "add_auto_import_dir": ht.add_auto_import_dir,
        "mount": ht.mount,
        "daemon": daemon,
        "graph": graph,
        "index": ht.index,
        "search_image": ht.search_image,
        "si": ht.search_image,
        "search": ht.search,
        "s": ht.search,
    }
    fire.Fire(fire_cli)


if __name__ == "__main__":
    main()
