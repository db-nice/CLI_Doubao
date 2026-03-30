/*
 * Copyright 2026
 *
 * Scan a smali directory tree and list classes that extend or implement a given type.
 */
package org.jf.baksmali;

import com.beust.jcommander.JCommander;
import com.beust.jcommander.Parameter;
import com.beust.jcommander.Parameters;
import org.jf.util.jcommander.Command;
import org.jf.util.jcommander.ExtendedParameter;
import org.jf.util.jcommander.ExtendedParameters;

import javax.annotation.Nonnull;
import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

@Parameters(commandDescription = "List subclasses/implementors of a class/interface from a smali directory.")
@ExtendedParameters(
        commandName = "subclasses",
        commandAliases = { "subclass", "children", "sub" })
public class SubclassesCommand extends Command {
    private enum SearchMode {
        AUTO,
        INHERITANCE,
        PACKAGE
    }

    @Parameter(names = {"-h", "-?", "--help"}, help = true,
            description = "Show usage information")
    private boolean help;

    @Parameter(names = {"--smali-dir", "--dir", "-d"}, required = true,
            description = "Root directory containing .smali files (baksmali output).")
    @ExtendedParameter(argumentNames = "path")
    private String smaliDir;

    @Parameter(names = {"--class", "--type", "-c"}, required = true,
            description = "Base class or package descriptor, for example Lpkg/Base; or Lpkg/base;")
    @ExtendedParameter(argumentNames = "descriptor")
    private String baseDescriptor;

    @Parameter(names = {"--transitive", "--recursive", "-r"}, arity = 1,
            description = "Include indirect subclasses. In package mode, also follows extends/implements chains. False by default.")
    @ExtendedParameter(argumentNames = "boolean")
    private boolean transitive = false;

    @Parameter(names = {"--mode", "-m"},
            description = "Search mode: auto, inheritance, package. Defaults to auto.")
    @ExtendedParameter(argumentNames = "mode")
    private String mode = SearchMode.AUTO.name().toLowerCase();

    @Parameter(names = {"-o", "--output"},
            description = "Write results to this file (UTF-8). If omitted, prints to stdout only.")
    @ExtendedParameter(argumentNames = "path")
    private String outputPath;

    public SubclassesCommand(@Nonnull List<JCommander> commandAncestors) {
        super(commandAncestors);
    }

    @Override public void run() {
        JCommander jc = getJCommander();
        if (help) {
            usage();
            return;
        }

        SearchMode searchMode = parseMode(mode);
        String normalizedInput = normalizeInput(baseDescriptor);
        if (normalizedInput == null) {
            throw new IllegalArgumentException("Invalid --class descriptor: " + baseDescriptor);
        }

        File root = new File(smaliDir);
        if (!root.isDirectory()) {
            throw new IllegalArgumentException("Not a directory: " + root.getAbsolutePath());
        }

        List<File> smaliFiles = listSmaliFiles(root);
        if (smaliFiles.isEmpty()) {
            throw new IllegalArgumentException("No .smali files under: " + root.getAbsolutePath());
        }

        Map<String, ClassHeader> headers = new HashMap<>();
        for (File file : smaliFiles) {
            ClassHeader header = readHeader(file);
            if (header != null && header.classDescriptor != null) {
                headers.put(header.classDescriptor, header);
            }
        }

        String resolvedInput = resolveDescriptor(headers, normalizedInput);

        SearchMode effectiveMode = resolveMode(searchMode, headers, resolvedInput);
        Set<String> result = effectiveMode == SearchMode.PACKAGE
                ? computePackageChildren(headers, resolvedInput, transitive)
                : computeInheritanceMatches(headers, resolvedInput, transitive);

        List<String> sorted = new ArrayList<>(result);
        sorted.sort(String::compareTo);

        writeOutput(sorted);
    }

    private void writeOutput(List<String> lines) {
        WriterSink sink = new WriterSink(outputPath);
        try {
            for (String line : lines) {
                sink.println(line);
            }
        } finally {
            sink.closeQuietly();
        }
    }

    private static Set<String> computeDirectChildren(Map<String, ClassHeader> headers, String base) {
        Set<String> out = new LinkedHashSet<>();
        for (ClassHeader h : headers.values()) {
            if (base.equals(h.superDescriptor)) {
                out.add(h.classDescriptor);
                continue;
            }
            if (h.implementsDescriptors != null && h.implementsDescriptors.contains(base)) {
                out.add(h.classDescriptor);
            }
        }
        return out;
    }

    private static Set<String> computeInheritanceMatches(Map<String, ClassHeader> headers, String base, boolean transitive) {
        Set<String> out = new LinkedHashSet<>();
        out.addAll(transitive ? computeTransitiveChildren(headers, base) : computeDirectChildren(headers, base));

        ClassHeader self = headers.get(base);
        if (self != null) {
            out.addAll(transitive ? computeTransitiveAncestors(headers, self) : computeDirectAncestors(self));
        }
        return out;
    }

    private static Set<String> computeTransitiveChildren(Map<String, ClassHeader> headers, String base) {
        return collectDescendants(buildReverseEdges(headers), base);
    }

    private static Set<String> computeDirectAncestors(ClassHeader header) {
        Set<String> out = new LinkedHashSet<>();
        if (header.superDescriptor != null) {
            out.add(header.superDescriptor);
        }
        if (header.implementsDescriptors != null) {
            out.addAll(header.implementsDescriptors);
        }
        return out;
    }

    private static Set<String> computeTransitiveAncestors(Map<String, ClassHeader> headers, ClassHeader header) {
        Set<String> out = new LinkedHashSet<>();
        Deque<String> queue = new ArrayDeque<>();
        if (header.superDescriptor != null) {
            queue.addLast(header.superDescriptor);
        }
        if (header.implementsDescriptors != null) {
            for (String descriptor : header.implementsDescriptors) {
                queue.addLast(descriptor);
            }
        }

        while (!queue.isEmpty()) {
            String current = queue.removeFirst();
            if (!out.add(current)) {
                continue;
            }
            ClassHeader parent = headers.get(current);
            if (parent == null) {
                continue;
            }
            if (parent.superDescriptor != null) {
                queue.addLast(parent.superDescriptor);
            }
            if (parent.implementsDescriptors != null) {
                for (String descriptor : parent.implementsDescriptors) {
                    queue.addLast(descriptor);
                }
            }
        }
        return out;
    }

    private static Set<String> computePackageChildren(Map<String, ClassHeader> headers, String base, boolean includeDescendants) {
        Set<String> out = new LinkedHashSet<>();
        String packagePrefix = toPackagePrefix(base);
        for (String descriptor : headers.keySet()) {
            if (descriptor.startsWith(packagePrefix)) {
                out.add(descriptor);
            }
        }
        if (!includeDescendants || out.isEmpty()) {
            return out;
        }

        Map<String, Set<String>> reverseEdges = buildReverseEdges(headers);
        Set<String> expanded = new LinkedHashSet<>(out);
        List<String> seeds = new ArrayList<>(out);
        for (String seed : seeds) {
            expanded.addAll(collectDescendants(reverseEdges, seed));
        }
        return expanded;
    }

    private static Map<String, Set<String>> buildReverseEdges(Map<String, ClassHeader> headers) {
        Map<String, Set<String>> reverseEdges = new HashMap<>();
        for (ClassHeader h : headers.values()) {
            if (h.superDescriptor != null) {
                reverseEdges.computeIfAbsent(h.superDescriptor, k -> new LinkedHashSet<>()).add(h.classDescriptor);
            }
            if (h.implementsDescriptors != null) {
                for (String itf : h.implementsDescriptors) {
                    reverseEdges.computeIfAbsent(itf, k -> new LinkedHashSet<>()).add(h.classDescriptor);
                }
            }
        }
        return reverseEdges;
    }

    private static Set<String> collectDescendants(Map<String, Set<String>> reverseEdges, String base) {
        Set<String> visited = new LinkedHashSet<>();
        Deque<String> q = new ArrayDeque<>();
        q.add(base);
        while (!q.isEmpty()) {
            String cur = q.removeFirst();
            Set<String> children = reverseEdges.get(cur);
            if (children == null) continue;
            for (String child : children) {
                if (visited.add(child)) {
                    q.addLast(child);
                }
            }
        }
        return visited;
    }

    private static List<File> listSmaliFiles(File root) {
        List<File> out = new ArrayList<>();
        Deque<File> stack = new ArrayDeque<>();
        stack.push(root);
        while (!stack.isEmpty()) {
            File dir = stack.pop();
            File[] files = dir.listFiles();
            if (files == null) continue;
            for (File f : files) {
                if (f.isDirectory()) {
                    stack.push(f);
                } else if (f.isFile() && f.getName().endsWith(".smali")) {
                    out.add(f);
                }
            }
        }
        return out;
    }

    private static ClassHeader readHeader(File smaliFile) {
        String cls = null;
        String sup = null;
        Set<String> impl = null;

        try (BufferedReader br = new BufferedReader(new InputStreamReader(new FileInputStream(smaliFile), StandardCharsets.UTF_8))) {
            String line;
            int linesRead = 0;
            while ((line = br.readLine()) != null) {
                linesRead++;
                // We only need the header area; bail out early once we hit members.
                if (line.startsWith("# direct methods") || line.startsWith("# virtual methods")
                        || line.startsWith(".method") || line.startsWith(".field")) {
                    break;
                }

                line = line.trim();
                if (line.startsWith(".class ")) {
                    cls = lastToken(line);
                } else if (line.startsWith(".super ")) {
                    sup = lastToken(line);
                } else if (line.startsWith(".implements ")) {
                    if (impl == null) impl = new LinkedHashSet<>();
                    impl.add(lastToken(line));
                }

                // Guard: avoid reading huge files if headers are malformed.
                if (linesRead > 2000) break;
            }
        } catch (IOException ex) {
            throw new RuntimeException("Failed reading: " + smaliFile.getAbsolutePath(), ex);
        }

        if (cls == null) return null;
        return new ClassHeader(cls, sup, impl);
    }

    private static String lastToken(String line) {
        int idx = line.lastIndexOf(' ');
        if (idx < 0) return line;
        return line.substring(idx + 1).trim();
    }

    private static SearchMode resolveMode(SearchMode requestedMode, Map<String, ClassHeader> headers, String input) {
        if (requestedMode != SearchMode.AUTO) {
            return requestedMode;
        }
        if (headers.containsKey(input)) {
            return SearchMode.INHERITANCE;
        }
        String packagePrefix = toPackagePrefix(input);
        for (String descriptor : headers.keySet()) {
            if (descriptor.startsWith(packagePrefix)) {
                return SearchMode.PACKAGE;
            }
        }
        return SearchMode.INHERITANCE;
    }

    private static String resolveDescriptor(Map<String, ClassHeader> headers, String input) {
        if (headers.containsKey(input)) {
            return input;
        }

        String dotToSlash = dotDescriptorToSlash(input);
        if (!dotToSlash.equals(input) && headers.containsKey(dotToSlash)) {
            return dotToSlash;
        }

        return dotToSlash;
    }

    private static SearchMode parseMode(String value) {
        if (value == null || value.trim().isEmpty()) {
            return SearchMode.AUTO;
        }
        String normalized = value.trim().toUpperCase().replace('-', '_');
        for (SearchMode candidate : SearchMode.values()) {
            if (candidate.name().equals(normalized)) {
                return candidate;
            }
        }
        throw new IllegalArgumentException("Unsupported --mode value: " + value);
    }

    private static String normalizeInput(String value) {
        if (value == null) {
            return null;
        }
        String normalized = value.trim().replace('\\', '/');
        if (normalized.isEmpty() || normalized.indexOf(' ') >= 0) {
            return null;
        }
        if (!normalized.startsWith("L")) {
            normalized = "L" + normalized;
        }
        if (!normalized.endsWith(";")) {
            normalized = normalized + ";";
        }
        return normalized;
    }

    private static String dotDescriptorToSlash(String value) {
        if (value.indexOf('/') >= 0 || value.indexOf('.') < 0) {
            return value;
        }
        return value.replace('.', '/');
    }

    private static String toPackagePrefix(String value) {
        String trimmed = value.substring(0, value.length() - 1);
        if (!trimmed.endsWith("/")) {
            trimmed = trimmed + "/";
        }
        return trimmed;
    }

    private static final class ClassHeader {
        final String classDescriptor;
        final String superDescriptor;
        final Set<String> implementsDescriptors;

        ClassHeader(String classDescriptor, String superDescriptor, Set<String> implementsDescriptors) {
            this.classDescriptor = classDescriptor;
            this.superDescriptor = superDescriptor;
            this.implementsDescriptors = implementsDescriptors;
        }
    }

    private static final class WriterSink {
        private final BufferedWriter fileWriter;

        WriterSink(String outputPath) {
            BufferedWriter fw = null;
            if (outputPath != null && !outputPath.isEmpty()) {
                try {
                    fw = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(new File(outputPath)), StandardCharsets.UTF_8));
                } catch (IOException ex) {
                    throw new RuntimeException("Failed opening output file: " + outputPath, ex);
                }
            }
            this.fileWriter = fw;
        }

        void println(String line) {
            System.out.println(line);
            if (fileWriter == null) return;
            try {
                fileWriter.write(line);
                fileWriter.newLine();
            } catch (IOException ex) {
                throw new RuntimeException("Failed writing output", ex);
            }
        }

        void closeQuietly() {
            if (fileWriter == null) return;
            try {
                fileWriter.flush();
                fileWriter.close();
            } catch (IOException ignored) {
            }
        }
    }
}
