package ma.dexter.util;



//import ma.dexter.dex.MutableClassDef;
import ma.dexter.ui.tree.TreeNode;
import ma.dexter.ui.tree.dex.DexClassNode;
import org.jf.baksmali.sposkosm.tools.MutableClassDef;
import org.jf.dexlib2.AccessFlags;
import org.jf.dexlib2.iface.ClassDef;
import org.jf.dexlib2.immutable.ImmutableClassDef;

import java.util.ArrayList;
import java.util.List;

public class DexUtils {

    public static final int DEFAULT_DEX_VERSION = 35;

    /**
     * Check if the class is an interface
     */
    public static boolean isInterface(MutableClassDef classDef) {
        return (classDef.getAccessFlags() & AccessFlags.INTERFACE.getValue()) != 0;
    }

    /**
     * Check if the class is an enum
     */
    public static boolean isEnum(MutableClassDef classDef) {
        return (classDef.getAccessFlags() & AccessFlags.ENUM.getValue()) != 0;
    }

    /**
     * Check if the class is an annotation
     */
    public static boolean isAnnotation(MutableClassDef classDef) {
        return (classDef.getAccessFlags() & AccessFlags.ANNOTATION.getValue()) != 0;
    }

    /**
     * Create a basic ClassDef with default values
     * @param classDescriptor The class descriptor (e.g., "Lcom/example/MyClass;")
     * @return A new ClassDef instance
     */
    public static ClassDef createClassDef(String classDescriptor) {
        return createClassDef(classDescriptor, "Ljava/lang/Object;");
    }

    /**
     * Create a basic ClassDef with specified superclass
     * @param classDescriptor The class descriptor (e.g., "Lcom/example/MyClass;")
     * @param superClassDescriptor The superclass descriptor
     * @return A new ClassDef instance
     */
    public static ClassDef createClassDef(String classDescriptor, String superClassDescriptor) {
        return new ImmutableClassDef(
                classDescriptor,           // type
                0,                         // access flags
                superClassDescriptor,      // superclass
                null,                      // interfaces
                null,                      // source file
                null,                      // annotations
                null,                      // fields
                null                       // methods
        );
    }

    /**
     * Get the package path from a TreeNode
     * @param treeNode The tree node
     * @return The package path as string with slashes
     */
    public static String getPath(TreeNode<DexClassNode> treeNode) {
        List<String> segments = new ArrayList<>();
        TreeNode<DexClassNode> node = treeNode;

        while (!node.isRoot()) {
            segments.add(node.getValue().getName());
            node = node.getParent();
        }

        // Reverse the list and join with dots, then replace with slashes
        StringBuilder path = new StringBuilder();
        for (int i = segments.size() - 1; i >= 0; i--) {
            path.append(segments.get(i));
            if (i > 0) {
                path.append(".");
            }
        }

        return path.toString().replace(".", "/");
    }

    /**
     * Get the class descriptor from a TreeNode
     * @param treeNode The tree node
     * @return The class descriptor (e.g., "Lcom/example/MyClass;")
     */
    public static String getClassDescriptor(TreeNode<DexClassNode> treeNode) {
        return "L" + getPath(treeNode) + ";";
    }

    /**
     * Normalize a smali path by removing leading 'L' and trailing ';'
     * @param classDefType The class descriptor (e.g., "Lcom/example/MyClass;")
     * @return The normalized path (e.g., "com/example/MyClass")
     */
    public static String normalizeSmaliPath(String classDefType) {
        if (classDefType.startsWith("L") && classDefType.endsWith(";")) {
            return classDefType.substring(1, classDefType.length() - 1);
        }
        return classDefType;
    }

    /**
     * Get the simple class name from a smali path
     * @param classDefType The class descriptor (e.g., "Lcom/example/MyClass;")
     * @return The simple class name (e.g., "MyClass")
     */
    public static String getClassNameFromSmaliPath(String classDefType) {
        String normalized = normalizeSmaliPath(classDefType);
        int lastSlash = normalized.lastIndexOf('/');
        if (lastSlash != -1) {
            return normalized.substring(lastSlash + 1);
        }
        return normalized;
    }

    /**
     * Get the package path from a class descriptor
     * @param classDefType The class descriptor (e.g., "Lcom/example/MyClass;")
     * @return The package path (e.g., "com/example/")
     */
    public static String getPackagePath(String classDefType) {
        String normalized = normalizeSmaliPath(classDefType);
        int lastSlash = normalized.lastIndexOf('/');
        if (lastSlash != -1) {
            return normalized.substring(0, lastSlash + 1); // Include the trailing slash
        }
        return "";
    }
}